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
        
        # Generated source files (perlasm, pnmtologo, etc.)
        self.generated_sources = {}  # output -> (generator_type, input, extra_args)

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
        """Parse ar command to extract object dependencies.
        
        Handles both standard ar commands and printf/xargs format:
        - Standard: ar cDPrST archive.a obj1.o obj2.o
        - Printf/xargs: printf "dir/%s " obj1.o obj2.o | xargs ar cDPrST archive.a
        """
        objs = []
        archive_dir = os.path.dirname(archive)
        
        # Check for printf/xargs format
        if 'printf' in command and 'xargs' in command:
            # Extract format string and object list
            # Format: printf "dir/%s " obj1.o obj2.o ... | xargs ar cDPrST archive.a
            import re

            # Find the printf format string (e.g., "arch/arm64/kernel/%s ")
            # Use word boundary to avoid matching 'printf' in variable names like 'savedcmd_lib/built-in.a'
            format_match = re.search(r'\bprintf\s+"([^"]+)"', command)
            if format_match:
                format_str = format_match.group(1)

                # Extract all object names after the format string (before | or xargs)
                # Objects are .o or .a files
                obj_pattern = r'(\S+\.(?:o|a))'

                # Find the part after printf "..." and before | or xargs
                # Use word boundary \b to match 'printf' as a command, not part of variable name
                parts_section = re.split(r'\bprintf', command)[-1].split('|')[0]
                # Remove the format string itself
                parts_section = re.sub(r'"[^"]+"', '', parts_section, count=1)

                # Find all object files
                for match in re.finditer(obj_pattern, parts_section):
                    obj_name = match.group(1)
                    # Apply format string
                    if '%s' in format_str:
                        obj_path = format_str.replace('%s', obj_name)
                    else:
                        obj_path = obj_name

                    # Strip whitespace from path
                    obj_path = obj_path.strip()

                    # Normalize path
                    obj_path = normalize_path(obj_path)
                    if archive_dir and not obj_path.startswith(archive_dir + '/'):
                        obj_path = os.path.join(archive_dir, obj_path)
                    obj_path = normalize_path(obj_path)
                    objs.append(obj_path)
                return objs
        
        # Standard ar command parsing
        parts = command.split()
        found_archive = False
        archive_basename = os.path.basename(archive)
        
        for part in parts:
            part = normalize_path(part)
            
            if part == archive or part == archive_basename:
                found_archive = True
                continue
            if found_archive and (part.endswith('.o') or part.endswith('.a')):
                if os.path.isabs(part):
                    obj_path = part
                elif archive_dir and archive_dir != '.':
                    if part.startswith(archive_dir + '/'):
                        obj_path = part
                    else:
                        obj_path = os.path.join(archive_dir, part)
                else:
                    obj_path = part
                obj_path = normalize_path(obj_path)
                objs.append(obj_path)
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
                self.vmlinux_a_deps = [d.lstrip('./') for d in deps if d.endswith('built-in.a')]
                # lib.a files go to vmlinux_libs
                self.vmlinux_libs = [d.lstrip('./') for d in deps if d.endswith('lib.a')]

        # Also check for existing vmlinux.a
        vmlinux_a = os.path.join(self.directory, 'vmlinux.a')
        if os.path.exists(vmlinux_a) and not self.vmlinux_a_deps:
            self.vmlinux_a_deps = [d.lstrip('./') for d in parse_archive_for_objs(vmlinux_a, self.ar) if d.endswith('built-in.a')]

        # Collect all lib.a files for KBUILD_VMLINUX_LIBS
        # These are libraries that need to be linked with vmlinux
        if not self.vmlinux_libs:
            self.vmlinux_libs = self._find_lib_a_files()

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
        # Generate link-vmlinux-fast.sh from link-vmlinux.sh
        self._generate_link_vmlinux_fast()
        
        with open(self.output, 'w', encoding='utf-8') as f:
            self._synthesize_missing_objcopy_rules()
            self._synthesize_missing_dtb_rules()
            self._find_all_generated_sources()  # Find generated sources before writing rules
            self._write_header(f)
            self._write_rules(f)
            self._write_generated_source_rules(f)
            self._write_compile_rules(f)
            self._write_objcopy_rules(f)
            self._write_archive_rules(f)
            self._write_header_rules(f)
            self._write_vmlinux_rules(f)
            self._write_modules_rules(f)
            self._write_default_target(f)

    def _write_generated_source_rules(self, f):
        """Write rules for generated source files (perlasm, pnmtologo, etc.)."""
        if not self.generated_sources:
            return
        
        f.write('# Generated source file rules\n')
        
        # Track if we need pnmtologo host program
        needs_pnmtologo = any(
            gen_type == 'pnmtologo' 
            for output, (gen_type, input_file, extra) in self.generated_sources.items()
        )
        
        # Build pnmtologo host program if needed
        if needs_pnmtologo:
            f.write('# Host program: pnmtologo\n')
            f.write('build drivers/video/logo/pnmtologo: hostcc drivers/video/logo/pnmtologo.c\n\n')
        
        for output, (gen_type, input_file, extra) in self.generated_sources.items():
            escaped_output = escape_ninja_path(output)
            escaped_input = escape_ninja_path(input_file)
            
            if gen_type == 'perlasm':
                # Perl generates .S from .pl
                # For sha2-armv8.pl, the script uses output filename to determine
                # whether to generate sha256 or sha512 code
                # Command: perl script.pl void output.S
                f.write(f'build {escaped_output}: perlasm_args {escaped_input}\n\n')
            
            elif gen_type == 'pnmtologo':
                # pnmtologo generates .c from .ppm/.pbm
                # Depends on the compiled pnmtologo host program
                logo_type = extra.get('type', 'clut224')
                logo_name = extra.get('name', 'logo')
                script = 'drivers/video/logo/pnmtologo'
                # Add implicit dependency on pnmtologo host program
                f.write(f'build {escaped_output}: pnmtologo {escaped_input} | {script}\n')
                f.write(f'  script = {script}\n')
                f.write(f'  type = {logo_type}\n')
                f.write(f'  name = {logo_name}\n\n')

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
                        # Unknown - try to find source file or generator
                        self._try_find_generator(obj, archive)

    def _try_find_generator(self, obj, archive):
        """Try to find a generator for a missing object file."""
        obj_dir = os.path.dirname(obj)
        obj_name = os.path.basename(obj).replace('.o', '')
        
        # Check for perlasm-generated .S files (crypto)
        # Pattern: dir/name-core.S generated from dir/name-armv8.pl or dir/name.pl
        # Special case: sha256-core and sha512-core both come from sha2-armv8.pl
        possible_pl_files = [
            os.path.join(obj_dir, obj_name + '-armv8.pl'),
            os.path.join(obj_dir, obj_name + '-armv4.pl'),
            os.path.join(obj_dir, obj_name + '.pl'),
            # Special case: sha256-core and sha512-core from sha2-armv8.pl
            os.path.join(obj_dir, 'sha2-armv8.pl'),
        ]
        for pl_file in possible_pl_files:
            if os.path.exists(pl_file):
                # Found a perl generator
                s_file = obj.replace('.o', '.S')
                self.generated_sources[s_file] = ('perlasm', pl_file, obj_name)
                # Now synthesize compile rule for .S -> .o
                self._synthesize_as_compile_rule(obj, s_file)
                return
        
        # Check for pnmtologo-generated .c files (logo)
        # Pattern: drivers/video/logo/logo_*.c from logo_*.ppm or logo_*.pbm
        if 'logo' in obj_dir and obj_name.startswith('logo_'):
            # Determine logo type from name
            if 'mono' in obj_name:
                logo_type = 'mono'
                ext = '.pbm'
            elif 'vga16' in obj_name:
                logo_type = 'vga16'
                ext = '.ppm'
            else:
                logo_type = 'clut224'
                ext = '.ppm'
            
            src_file = os.path.join(obj_dir, obj_name + ext)
            if os.path.exists(src_file):
                c_file = obj.replace('.o', '.c')
                self.generated_sources[c_file] = ('pnmtologo', src_file, {'type': logo_type, 'name': obj_name})
                self._synthesize_c_compile_rule(obj, c_file)
                return
        
        # Check for .asn1 generated files
        if '.asn1' in obj_name:
            # ASN.1 files are generated during build - they need special handling
            # For now, mark them as needing fallback
            return
        
        # Try to find source file
        for ext in ['.c', '.S']:
            src_path = os.path.join(obj_dir, obj_name + ext)
            if os.path.exists(src_path):
                if ext == '.S':
                    self._synthesize_as_compile_rule(obj, src_path)
                else:
                    self._synthesize_c_compile_rule(obj, src_path)
                return
    
    def _synthesize_as_compile_rule(self, obj, s_file):
        """Synthesize an assembly compile rule."""
        if self.compile_rules:
            # Find an existing .S compile rule as template
            for sample_target, (sample_src, sample_cmd) in self.compile_rules.items():
                if sample_src and sample_src.endswith('.S'):
                    synthesized_cmd = sample_cmd.replace(sample_target, obj)
                    synthesized_cmd = re.sub(r'\S+\.S$', s_file, synthesized_cmd)
                    self.compile_rules[obj] = (s_file, synthesized_cmd)
                    return
        # Fallback: create a simple rule
        self.compile_rules[obj] = (s_file, f'gcc -c -o {obj} {s_file}')
    
    def _synthesize_c_compile_rule(self, obj, c_file):
        """Synthesize a C compile rule."""
        if self.compile_rules:
            # Find an existing .c compile rule as template
            for sample_target, (sample_src, sample_cmd) in self.compile_rules.items():
                if sample_src and sample_src.endswith('.c'):
                    synthesized_cmd = sample_cmd.replace(sample_target, obj)
                    synthesized_cmd = re.sub(r'\S+\.c$', c_file, synthesized_cmd)
                    self.compile_rules[obj] = (c_file, synthesized_cmd)
                    return
        # Fallback: create a simple rule
        self.compile_rules[obj] = (c_file, f'gcc -c -o {obj} {c_file}')

    def _try_find_generator_for_source(self, source_path):
        """Try to find a generator for a source file that doesn't exist yet.
        
        This handles cases like:
        - sha256-core.S from sha2-armv8.pl (perlasm)
        - logo_linux_clut224.c from logo_linux_clut224.ppm (pnmtologo)
        """
        src_dir = os.path.dirname(source_path)
        src_name = os.path.basename(source_path)
        src_base, src_ext = os.path.splitext(src_name)
        
        # Check for perlasm-generated .S files (crypto)
        if src_ext == '.S':
            # Pattern: name-core.S generated from name-armv8.pl or name.pl
            # Also handle sha2-armv8.pl which generates sha256-core.S and sha512-core.S
            possible_pl_files = [
                os.path.join(src_dir, src_base + '-armv8.pl'),
                os.path.join(src_dir, src_base + '-armv4.pl'),
                os.path.join(src_dir, src_base + '.pl'),
                # Special case: sha256-core.S and sha512-core.S both come from sha2-armv8.pl
                os.path.join(src_dir, 'sha2-armv8.pl'),
            ]
            for pl_file in possible_pl_files:
                if os.path.exists(pl_file):
                    # Found a perl generator
                    self.generated_sources[source_path] = ('perlasm', pl_file, src_base)
                    return
        
        # Check for pnmtologo-generated .c files (logo)
        if src_ext == '.c' and 'logo' in src_dir and src_name.startswith('logo_'):
            # Determine logo type from name
            if 'mono' in src_name:
                logo_type = 'mono'
                logo_ext = '.pbm'
            elif 'vga16' in src_name:
                logo_type = 'vga16'
                logo_ext = '.ppm'
            else:
                logo_type = 'clut224'
                logo_ext = '.ppm'
            
            src_file = os.path.join(src_dir, src_base + logo_ext)
            if os.path.exists(src_file):
                self.generated_sources[source_path] = ('pnmtologo', src_file, {'type': logo_type, 'name': src_base})

    def _find_all_generated_sources(self):
        """Find all source files that need to be generated before compilation.
        
        This scans all compile rules to find sources that don't exist
        and tries to find generators for them.
        """
        for obj, (source, _) in self.compile_rules.items():
            if not source:
                continue
            
            # Remove ../ prefix if present
            source_path = source[3:] if source.startswith('../') else source
            
            if not os.path.exists(source_path):
                self._try_find_generator_for_source(source_path)

    def _write_header(self, f):
        """Write ninja file header."""
        f.write('# Generated by generate_ninja.py\n')
        f.write('# Do not edit manually\n\n')
        f.write(f'builddir = {escape_ninja_path(self.directory)}\n')
        f.write(f'srctree = {escape_ninja_path(self._find_srctree())}\n')
        f.write(f'AR = {self.ar}\n')
        f.write(f'CC = {self.cc}\n\n')

    def _generate_link_vmlinux_fast(self):
        """Generate link-vmlinux-fast.sh from link-vmlinux.sh by removing make dependency."""
        src_script = os.path.join(self.directory, 'scripts', 'link-vmlinux.sh')
        dst_script = os.path.join(self.directory, 'scripts', 'link-vmlinux-fast.sh')
        
        if not os.path.exists(src_script):
            return
        
        try:
            with open(src_script, 'r', encoding='utf-8') as f:
                content = f.read()
            
            # Remove the make command line that builds init/version-timestamp.o
            lines = content.split('\n')
            filtered_lines = []
            skip_next = False
            
            for i, line in enumerate(lines):
                if skip_next:
                    skip_next = False
                    continue
                
                # Skip the ${MAKE} line for init/version-timestamp.o
                if '${MAKE} -f' in line and 'init/version-timestamp.o' in line:
                    # Also skip the empty line after it if present
                    skip_next = True
                    continue
                
                # Fix the .d file generation at the end
                if 'echo "${VMLINUX}: $0" > ".${VMLINUX}.d"' in line:
                    # Replace with the fixed version using objtree
                    line = '# Use relative path for .d file\nvmlinux_basename=$(basename "${VMLINUX}")\necho "${VMLINUX}: $0" > "${objtree}/.${vmlinux_basename}.d"'
                
                filtered_lines.append(line)
            
            new_content = '\n'.join(filtered_lines)
            
            with open(dst_script, 'w', encoding='utf-8') as f:
                f.write(new_content)
            
            # Make it executable
            os.chmod(dst_script, 0o755)
            
        except (IOError, OSError) as e:
            pass

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

        # Archive rule - use regular archive (no T flag)
        # Nested archives are expanded to their member object files
        f.write('rule ar\n')
        f.write('  command = rm -f $out && $AR rcs $out $in\n')
        f.write('  description = AR $out\n\n')

        # Empty archive rule (creates an empty archive)
        f.write('rule ar_empty\n')
        f.write('  command = rm -f $out && $AR rcs $out\n')
        f.write('  description = AR $out (empty)\n\n')

        # Link rule for object files
        f.write('rule ld\n')
        f.write('  command = $cmd\n')
        f.write('  description = LD $out\n\n')

        # Link rule for vmlinux.o (matching scripts/Makefile.vmlinux_o)
        f.write('rule ld_vmlinux_o\n')
        f.write('  command = $LD $KBUILD_LDFLAGS -r -o $out --whole-archive $in --no-whole-archive --start-group $libs --end-group\n')
        f.write('  description = LD $out\n\n')

        # Link rule for vmlinux
        f.write('rule ld_vmlinux\n')
        f.write('  command = $cmd\n')
        f.write('  description = LD $out\n\n')

        # Header generation rules (pure shell, no Make syntax)
        f.write('rule gen_utsrelease_h\n')
        f.write('  command = printf "#define UTS_RELEASE \\"%s\\"\\n" "$$(cat include/config/kernel.release 2>/dev/null || echo unknown)" > $out\n')
        f.write('  description = GEN $out\n\n')

        f.write('rule gen_compile_h\n')
        cmd = """  command = sh -c '$srctree/scripts/mkcompile_h "$$(uname -m)" "$$(gcc --version 2>/dev/null | head -1)" "$${LD:-ld}" > $out'\n"""
        f.write(cmd)
        f.write('  description = GEN $out\n\n')

        f.write('rule gen_utsversion_h\n')
        f.write('  command = utsver="$$(cat include/config/kernel.release 2>/dev/null || echo unknown)"; if grep -q "CONFIG_SMP=y" include/config/auto.conf 2>/dev/null; then utsver="$$utsver SMP"; fi; if grep -q "CONFIG_PREEMPT=y" include/config/auto.conf 2>/dev/null; then utsver="$$utsver PREEMPT"; fi; utsver="$$utsver $$(date +%Y-%m-%d)"; utsver=$$(echo "$$utsver" | cut -c1-64); printf "#define UTS_VERSION \\"%s\\"\\n" "$$utsver" > $out\n')
        f.write('  description = GEN $out\n\n')

        # Kallsyms rule
        f.write('rule kallsyms\n')
        f.write('  command = scripts/kallsyms $in > $out\n')
        f.write('  description = KALLSYMS $out\n\n')

        # Modpost rule
        f.write('rule modpost\n')
        f.write('  command = scripts/mod/modpost $args\n')
        f.write('  description = MODPOST\n\n')

        # Vmlinux export.c generation rule (creates empty export file)
        f.write('rule vmlinux_export_c\n')
        f.write('  command = echo "#include <linux/export-internal.h>" > $out\n')
        f.write('  description = GEN $out\n\n')

        # Vmlinux export.o generation rule (creates empty object file)
        f.write('rule vmlinux_export_o\n')
        f.write('  command = echo \'.section .note.GNU-stack,"",@progbits\' | $CC -c -x assembler -o $out -\n')
        f.write('  description = AS $out\n\n')

        # Module link rule
        f.write('rule ld_ko\n')
        f.write('  command = $LD -r -o $out $in\n')
        f.write('  description = LD [M] $out\n\n')

        # Objcopy rule (for transformations like .o -> .pi.o)
        f.write('rule objcopy\n')
        f.write('  command = $cmd\n')
        f.write('  description = OBJCOPY $out\n\n')

        # Perl ASM generator rule (for crypto .S files generated from .pl)
        f.write('rule perlasm\n')
        f.write('  command = perl $in > $out\n')
        f.write('  description = PERLASM $out\n\n')

        # Perl ASM with args rule (for crypto .S files with extra args)
        # The script uses output filename to determine what to generate (sha256 vs sha512)
        # Command: perl script.pl void output.S
        f.write('rule perlasm_args\n')
        f.write('  command = perl $in void $out\n')
        f.write('  description = PERLASM $out\n\n')

        # Host program compilation rule
        f.write('rule hostcc\n')
        f.write('  command = gcc -Wall -O2 -o $out $in\n')
        f.write('  description = HOSTCC $out\n\n')

        # PNM to logo converter rule
        # pnmtologo is a host program that needs to be compiled first
        f.write('rule pnmtologo\n')
        f.write('  command = $script -t $type -n $name -o $out $in\n')
        f.write('  description = LOGO $out\n\n')

        # Vmlinux final link rule - calls link-vmlinux-fast.sh
        f.write('rule vmlinux_link\n')
        f.write('  command = srctree=$srctree objtree=$objtree ARCH=$ARCH SRCARCH=$SRCARCH ')
        f.write('LD="$LD" NM="$NM" OBJCOPY="$OBJCOPY" OBJDUMP="$OBJDUMP" STRIP="$STRIP" CC="$CC" ')
        f.write('KBUILD_LDFLAGS="$KBUILD_LDFLAGS" LDFLAGS_vmlinux="$LDFLAGS_vmlinux" ')
        f.write('KBUILD_VMLINUX_LIBS="$KBUILD_VMLINUX_LIBS" KBUILD_LDS="$KBUILD_LDS" ')
        f.write('PAHOLE="$PAHOLE" PAHOLE_FLAGS="$PAHOLE_FLAGS" ')
        f.write('RESOLVE_BTFIDS="$RESOLVE_BTFIDS" ')
        f.write('NOSTDINC_FLAGS="$NOSTDINC_FLAGS" LINUXINCLUDE="$LINUXINCLUDE" ')
        f.write('KBUILD_CPPFLAGS="$KBUILD_CPPFLAGS" KBUILD_AFLAGS="$KBUILD_AFLAGS" ')
        f.write('KBUILD_AFLAGS_KERNEL="$KBUILD_AFLAGS_KERNEL" ')
        f.write('CONFIG_SHELL="$CONFIG_SHELL" ')
        f.write('sh scripts/link-vmlinux-fast.sh "$LD" "$KBUILD_LDFLAGS" "$LDFLAGS_vmlinux" $out\n')
        f.write('  description = LINK vmlinux\n\n')

        # Fallback rule for targets without known rules (complex link targets, etc.)
        # Uses flock to serialize make calls to avoid race conditions with kernel.release
        f.write('rule fallback\n')
        f.write('  command = flock /tmp/linux-ninja.lock -c "make $target"\n')
        f.write('  description = FALLBACK MAKE $target\n\n')

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
            # Skip version-timestamp.o as we have a custom rule for it
            if 'version-timestamp.o' in obj:
                continue
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

            # Check if source file is generated (doesn't exist or in generated_sources)
            source_path = source[3:] if source.startswith('../') else source
            implicit_deps = []
            if source_path in self.generated_sources:
                # Add the generated source as an implicit dependency
                # This ensures the generator runs before compilation
                implicit_deps.append(escaped_src)

            # Write build rule with optional implicit dependencies
            if implicit_deps:
                f.write(f'build {escaped_obj}: {rule} {escaped_src} | {" ".join(implicit_deps)}\n')
            else:
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

    def _write_fallback_rule(self, f, target):
        """Generate a fallback rule that calls make for targets without known rules.
        
        This handles complex link targets like kvm_nvhe.o that are generated
        through multi-step link processes not captured by .cmd files.
        """
        escaped_target = escape_ninja_path(target)
        f.write(f'build {escaped_target}: fallback\n')
        f.write(f'  target = {target}\n\n')

    def _expand_archive_members(self, archive_path, visited=None):
        """Recursively expand an archive to get all member object files.
        
        For thin archives with nested archives, this extracts the actual
        object files rather than adding the nested archive as a member.
        
        Returns a list of object file paths.
        """
        if visited is None:
            visited = set()
        
        # Prevent infinite recursion
        real_path = os.path.realpath(archive_path)
        if real_path in visited:
            return []
        visited.add(real_path)
        
        objs = []
        try:
            # Get members of this archive
            output = subprocess.check_output(
                [self.ar, '-t', archive_path],
                stderr=subprocess.DEVNULL
            )
            members = output.decode().strip().split()
            archive_dir = os.path.dirname(archive_path)
            
            for member in members:
                if not member:
                    continue
                # Resolve to full path
                if os.path.isabs(member):
                    member_path = member
                else:
                    member_path = os.path.join(archive_dir, member)
                member_path = normalize_path(member_path)
                
                if member_path.endswith('.a'):
                    # Nested archive - recursively expand
                    if os.path.exists(member_path):
                        objs.extend(self._expand_archive_members(member_path, visited))
                elif member_path.endswith('.o'):
                    # Object file - add to list
                    objs.append(member_path)
        except (subprocess.CalledProcessError, OSError):
            pass
        
        return objs

    def _expand_nested_archive(self, archive_name, normalized_archive_rules, visited=None):
        """Recursively expand an archive to get all object file paths.
        
        This operates on the normalized_archive_rules dictionary to avoid
        depending on archives already being built.
        
        Returns a list of object file paths.
        """
        if visited is None:
            visited = set()
        
        if archive_name in visited:
            return []
        visited.add(archive_name)
        
        objs = []
        if archive_name not in normalized_archive_rules:
            return objs
        
        archive_dir = os.path.dirname(archive_name)
        
        for obj in normalized_archive_rules[archive_name]:
            # Skip init/version.o - it will be replaced by version-timestamp.o in final link
            if obj.endswith('init/version.o'):
                continue
            if obj.endswith('.o'):
                # Resolve relative paths - only join if obj is not already an absolute-like path
                if not os.path.isabs(obj) and archive_dir and not obj.startswith(archive_dir + '/'):
                    obj = os.path.join(archive_dir, obj)
                objs.append(obj)
            elif obj.endswith('.a'):
                # Recursively expand nested archive
                # Resolve relative path to full archive name
                if not os.path.isabs(obj) and archive_dir and not obj.startswith(archive_dir + '/'):
                    nested_archive = os.path.join(archive_dir, obj)
                else:
                    nested_archive = obj
                objs.extend(self._expand_nested_archive(nested_archive, normalized_archive_rules, visited))
        
        return objs

    def _write_archive_rules(self, f):
        """Write archive rules for built-in.a files."""
        f.write('# Archive rules\n')

        # Track targets that already have fallback rules to avoid duplicates
        fallback_written = set()

        # Helper to normalize path (remove leading ./)
        def normalize_path(p):
            if p.startswith('./'):
                return p[2:]
            return p

        # Normalize archive_rules keys
        normalized_archive_rules = {}
        for archive, objs in self.archive_rules.items():
            norm_archive = normalize_path(archive)
            norm_objs = [normalize_path(obj) for obj in objs]
            normalized_archive_rules[norm_archive] = norm_objs

        for archive in sorted(normalized_archive_rules.keys()):
            objs = normalized_archive_rules[archive]

            # Check for missing dependencies and generate fallback rules
            # Exclude init/version.o - it will be replaced by version-timestamp.o in final link
            valid_objs = []
            for obj in sorted(objs):
                # Skip init/version.o - it will be replaced by version-timestamp.o
                if obj.endswith('init/version.o'):
                    continue
                # Check if this object has a compile rule, objcopy rule, or is another archive
                if obj in self.compile_rules or obj in self.objcopy_rules:
                    valid_objs.append(obj)
                elif obj.endswith('.a'):
                    # Archive dependency - expand nested archives to their members
                    if obj in normalized_archive_rules:
                        # Archive has build rules - recursively expand to get all object files
                        # This is preferred over reading existing archive files because
                        # normalized_archive_rules contains proper path information
                        expanded_objs = self._expand_nested_archive(obj, normalized_archive_rules)
                        valid_objs.extend(expanded_objs)
                    else:
                        archive_path = os.path.join(self.directory, obj)
                        if os.path.exists(archive_path):
                            # Archive exists but no build rules - expand it recursively
                            expanded_objs = self._expand_archive_members(archive_path)
                            valid_objs.extend(expanded_objs)
                        elif obj not in fallback_written:
                            # Missing archive - generate fallback rule
                            self._write_fallback_rule(f, obj)
                            fallback_written.add(obj)
                            valid_objs.append(obj)
                        else:
                            valid_objs.append(obj)
                elif obj not in fallback_written:
                    # Missing rule - generate fallback
                    self._write_fallback_rule(f, obj)
                    fallback_written.add(obj)
                    valid_objs.append(obj)
                else:
                    # Already has fallback rule written
                    valid_objs.append(obj)
            
            # Remove duplicates while preserving order
            seen = set()
            deduped_objs = []
            for obj in valid_objs:
                if obj not in seen:
                    seen.add(obj)
                    deduped_objs.append(obj)
            valid_objs = deduped_objs

            escaped_archive = escape_ninja_path(archive)

            if valid_objs:
                escaped_objs = ' '.join(escape_ninja_path(obj) for obj in valid_objs)
                f.write(f'build {escaped_archive}: ar {escaped_objs}\n\n')
            else:
                # Empty archive - create empty .a file using ar cq
                f.write(f'build {escaped_archive}: ar_empty\n\n')

    def _write_header_rules(self, f):
        """Write rules for generated headers and version-timestamp.o."""
        f.write('# Generated headers and version-timestamp.o rules\n')
        auto_conf = os.path.join(self.directory, 'include/config/auto.conf')
        kernel_release = os.path.join(self.directory, 'include/config/kernel.release')
        if os.path.exists(kernel_release):
            f.write('build include/generated/utsrelease.h: gen_utsrelease_h include/config/kernel.release\n\n')
        else:
            f.write('build include/generated/utsrelease.h: gen_utsrelease_h\n\n')
        if os.path.exists(auto_conf):
            f.write('build include/generated/compile.h: gen_compile_h | include/config/auto.conf\n\n')
            f.write('build include/generated/utsversion.h: gen_utsversion_h | include/config/auto.conf\n\n')
        else:
            f.write('build include/generated/compile.h: gen_compile_h\n\n')
            f.write('build include/generated/utsversion.h: gen_utsversion_h\n\n')
        # Use relative paths for consistency with header rules
        version_ts_o = 'init/version-timestamp.o'
        version_c = 'init/version.c'
        utsversion_h = 'include/generated/utsversion.h'
        utsrelease_h = 'include/generated/utsrelease.h'
        compile_h = 'include/generated/compile.h'
        # compile.h is normal dependency (before |), utsversion.h and utsrelease_h are order-only (after |)
        f.write(f'build {version_ts_o}: cc {version_c} {compile_h} | {utsversion_h} {utsrelease_h}\n')
        # Extract compile command from version.o.cmd and modify for version-timestamp.o
        version_cmd_file = os.path.join(self.directory, 'init/.version.o.cmd')
        if os.path.exists(version_cmd_file):
            with open(version_cmd_file, 'r') as vcf:
                cmd_line = vcf.readline().strip()
            # Remove 'savedcmd_init/version.o := ' prefix
            if ':=' in cmd_line:
                cmd = cmd_line.split(':= ', 1)[1]
            else:
                cmd = cmd_line
            # Modify output file and includes
            cmd = cmd.replace('-o init/version.o', '-o init/version-timestamp.o')
            # Replace utsversion-tmp.h with multiple -include directives
            cmd = cmd.replace('init/utsversion-tmp.h', 'include/generated/utsversion.h')
            # Add additional includes before the -D flags
            cmd = cmd.replace('    -DKBUILD_MODFILE=', ' -include include/generated/utsrelease.h -include include/generated/compile.h    -DKBUILD_MODFILE=')
            cmd = cmd.replace("'-DKBUILD_MODFILE=\"init/version\"'", "'-DKBUILD_MODFILE=\"init/version-timestamp\"'")
            cmd = cmd.replace("'-DKBUILD_BASENAME=\"version\"'", "'-DKBUILD_BASENAME=\"version-timestamp\"'")
            cmd = cmd.replace("'-DKBUILD_MODNAME=\"version\"'", "'-DKBUILD_MODNAME=\"version-timestamp\"'")
            cmd = cmd.replace('__KBUILD_MODNAME=kmod_version', '__KBUILD_MODNAME=kmod_version_timestamp')
            cmd = escape_ninja_cmd(cmd)
        else:
            # Fallback to simple command if cmd file doesn't exist
            cmd = f'$CC -c -o {version_ts_o} {version_c} -include {utsversion_h} -include {utsrelease_h} -include {compile_h}'
        f.write(f'  cmd = {cmd}\n\n')

    def _write_vmlinux_rules(self, f):
        """Write rules for vmlinux build chain."""
        if not self.vmlinux_a_deps:
            return

        f.write('# vmlinux build rules\n')
        
        # Build normalized_archive_rules for expanding nested archives
        def normalize_path(p):
            if p.startswith('./'):
                return p[2:]
            return p
        
        normalized_archive_rules = {}
        for archive, objs in self.archive_rules.items():
            norm_archive = normalize_path(archive)
            norm_objs = [normalize_path(obj) for obj in objs]
            normalized_archive_rules[norm_archive] = norm_objs
        
        # Detect architecture from config
        arch = self._get_arch()
        srcarch = arch
        # Handle special cases where SRCARCH differs from ARCH
        if arch == 'x86_64':
            srcarch = 'x86'
        
        # Get architecture-specific settings
        ld_cmd = self._get_ld_cmd(arch)
        kbuild_ldflags = self._get_kbuild_ldflags(arch)
        lds_path = f'arch/{srcarch}/kernel/vmlinux.lds'
        linuxinclude = self._get_linuxinclude(srcarch)
        
        # Write ninja variables for vmlinux link
        f.write('# Variables for vmlinux link\n')
        f.write('srctree = .\n')
        f.write('objtree = .\n')
        f.write(f'ARCH = {arch}\n')
        f.write(f'SRCARCH = {srcarch}\n')
        f.write(f'LD = {ld_cmd}\n')
        f.write('NM = nm\n')
        f.write('OBJCOPY = objcopy\n')
        f.write('OBJDUMP = objdump\n')
        f.write('STRIP = strip\n')
        f.write('CC = gcc\n')
        f.write(f'KBUILD_LDFLAGS = {kbuild_ldflags}\n')
        f.write('LDFLAGS_vmlinux = -X --pic-veneer -Bsymbolic -z notext --no-apply-dynamic-reloc --build-id=sha1 --orphan-handling=warn\n')
        escaped_libs = ' '.join(escape_ninja_path(lib) for lib in sorted(self.vmlinux_libs))
        f.write(f'KBUILD_VMLINUX_LIBS = {escaped_libs}\n')
        f.write(f'KBUILD_LDS = {lds_path}\n')
        f.write('PAHOLE = pahole\n')
        f.write('PAHOLE_FLAGS = \n')
        f.write('RESOLVE_BTFIDS = ./tools/bpf/resolve_btfids/resolve_btfids\n')
        f.write('NOSTDINC_FLAGS = -nostdinc\n')
        f.write(f'LINUXINCLUDE = {linuxinclude}\n')
        f.write('KBUILD_CPPFLAGS = \n')
        f.write('KBUILD_AFLAGS = -D__ASSEMBLY__\n')
        f.write('KBUILD_AFLAGS_KERNEL = \n')
        f.write('CONFIG_SHELL = /bin/sh\n\n')

        # Linker script preprocessing rule (.lds.S -> .lds)
        f.write('rule lds_preproc\n')
        f.write('  command = $CC -E -nostdinc $LINUXINCLUDE -D__KERNEL__ -P -U$SRCARCH -D__ASSEMBLY__ -DLINKER_SCRIPT -o $out $in\n')
        f.write('  description = LDS $out\n\n')

        # vmlinux.a from all built-in.a and lib.a files
        # Expand all nested archives to get individual object files
        vmlinux_a = os.path.join(self.directory, 'vmlinux.a')
        escaped_vmlinux_a = escape_ninja_path(vmlinux_a)
        
        # Collect all object files from built-in.a deps (recursively expanding nested archives)
        # Note: lib.a files are NOT included in vmlinux.a, they are linked as KBUILD_VMLINUX_LIBS
        all_vmlinux_objs = []
        for dep in sorted(self.vmlinux_a_deps):
            if dep in normalized_archive_rules:
                # Expand this archive and its nested archives
                expanded = self._expand_nested_archive(dep, normalized_archive_rules)
                all_vmlinux_objs.extend(expanded)
            elif dep.endswith('.a') and os.path.exists(os.path.join(self.directory, dep)):
                # Archive exists but no build rules - expand using ar
                archive_path = os.path.join(self.directory, dep)
                expanded = self._expand_archive_members(archive_path)
                all_vmlinux_objs.extend(expanded)
            else:
                all_vmlinux_objs.append(dep)
        
        # Remove duplicates while preserving order
        seen = set()
        deduped_objs = []
        for obj in all_vmlinux_objs:
            if obj not in seen:
                seen.add(obj)
                deduped_objs.append(obj)
        
        escaped_deps = ' '.join(escape_ninja_path(obj) for obj in deduped_objs)
        f.write(f'build {escaped_vmlinux_a}: ar {escaped_deps}\n\n')

        # vmlinux.o from vmlinux.a and lib.a files
        # Note: version-timestamp.o is NOT linked here, it's linked in the final vmlinux step
        vmlinux_o = os.path.join(self.directory, 'vmlinux.o')
        escaped_vmlinux_o = escape_ninja_path(vmlinux_o)
        escaped_libs = ' '.join(escape_ninja_path(lib) for lib in sorted(self.vmlinux_libs))

        # Add lib.a files as implicit dependencies (after |) so they are built before linking
        # but are not passed as input to the linker command directly
        implicit_deps = ''
        if self.vmlinux_libs:
            implicit_deps = ' | ' + ' '.join(escape_ninja_path(lib) for lib in sorted(self.vmlinux_libs))

        f.write(f'build {escaped_vmlinux_o}: ld_vmlinux_o {escaped_vmlinux_a}{implicit_deps}\n')
        f.write(f'  libs = {escaped_libs}\n')
        f.write(f'  LD = /usr/bin/aarch64-linux-gnu-ld.bfd\n')
        f.write(f'  KBUILD_LDFLAGS = -EL -maarch64elf -z noexecstack --no-warn-rwx-segments\n\n')

        # .vmlinux.export.o generation (always generate, modpost will create empty one if no exports)
        export_c = os.path.join(self.directory, '.vmlinux.export.c')
        export_o = os.path.join(self.directory, '.vmlinux.export.o')
        escaped_export_c = escape_ninja_path(export_c)
        escaped_export_o = escape_ninja_path(export_o)
        escaped_vmlinux_o_dep = escape_ninja_path(os.path.join(self.directory, 'vmlinux.o'))
        
        f.write(f'# Generate .vmlinux.export.c (empty export file)\n')
        f.write(f'build {escaped_export_c}: vmlinux_export_c {escaped_vmlinux_o_dep}\n\n')
        
        f.write(f'# Create empty .vmlinux.export.o\n')
        f.write(f'build {escaped_export_o}: vmlinux_export_o {escaped_export_c}\n\n')

        # vmlinux final link using link-vmlinux-fast.sh
        vmlinux = os.path.join(self.directory, 'vmlinux')
        lds_file = self._find_linker_script()
        
        # Check if linker script needs preprocessing (.lds.S -> .lds)
        # Normalize lds_file to relative path without leading ./
        if lds_file:
            if lds_file.startswith('./'):
                lds_file = lds_file[2:]
            elif lds_file.startswith('/'):
                lds_file = os.path.relpath(lds_file, self.directory)
        
        if lds_file and lds_file.endswith('.lds.S'):
            lds_output = lds_file[:-2]  # Remove .S extension
            escaped_lds_s = escape_ninja_path(lds_file)
            escaped_lds = escape_ninja_path(lds_output)
            f.write(f'build {escaped_lds}: lds_preproc {escaped_lds_s}\n\n')
            lds_file = lds_output
        
        # Use relative path for lds
        default_lds = f'arch/{srcarch}/kernel/vmlinux.lds'
        lds_path = lds_file if lds_file else default_lds
        escaped_lds_path = escape_ninja_path(lds_path)
        version_ts_o = 'init/version-timestamp.o'
        escaped_version_ts_o = escape_ninja_path(version_ts_o)
        # Use vmlinux.a (not vmlinux.o) for final link, matching make behavior
        escaped_vmlinux_a = escape_ninja_path(os.path.join(self.directory, 'vmlinux.a'))
        # vmlinux link - inputs are vmlinux.a, version-timestamp.o, .vmlinux.export.o, and linker script
        lds_dep = f' {escaped_lds_path}' if lds_path else ''
        f.write(f'build {escape_ninja_path(vmlinux)}: vmlinux_link {escaped_vmlinux_a} {escaped_version_ts_o} {escaped_export_o}{lds_dep}\n')
        f.write(f'  vmlinux_a = {escaped_vmlinux_a}\n')
        f.write(f'  version_ts = {escaped_version_ts_o}\n')
        f.write(f'  lds = {escaped_lds_path}\n\n')

    def _find_srctree(self):
        """Find the source tree directory."""
        # For in-tree builds, srctree == objtree
        # For out-of-tree builds, we need to find the source tree
        # Check if there's a source symlink or we can detect from .cmd files
        if hasattr(self, 'compile_rules') and self.compile_rules:
            for obj, (source, _) in self.compile_rules.items():
                if source and source.startswith('../'):
                    # Out-of-tree build, remove the ../ prefix to get srctree-relative path
                    # The srctree is the parent of objtree
                    return os.path.dirname(os.path.realpath(self.directory))
        # Default: in-tree build
        return self.directory

    def _find_lib_a_files(self):
        """Find all lib.a files for KBUILD_VMLINUX_LIBS.
        
        These are typically in:
        - lib/lib.a
        - arch/<arch>/lib/lib.a
        """
        lib_files = []
        
        # Check common lib.a locations - look for .cmd files since lib.a may not be built yet
        common_libs = [
            'lib/lib.a',
            f'arch/{self._get_arch()}/lib/lib.a',
        ]
        
        for lib in common_libs:
            # .cmd file is in the same directory as lib.a, named .lib.a.cmd
            lib_dir = os.path.dirname(lib)
            lib_base = os.path.basename(lib)
            lib_cmd = os.path.join(self.directory, lib_dir, f'.{lib_base}.cmd')
            if os.path.exists(lib_cmd):
                lib_files.append(lib)
        
        # Also search for any other lib.a files with .cmd files
        for root, dirs, files in os.walk(self.directory):
            # Skip excluded directories
            if any(excl in root for excl in _EXCLUDE_DIRS):
                continue
            if '.lib.a.cmd' in files:
                lib_path = os.path.join(root, 'lib.a')
                rel_path = os.path.relpath(lib_path, self.directory)
                if rel_path not in lib_files:
                    lib_files.append(rel_path)
        
        return lib_files

    def _get_arch(self):
        """Get the architecture from .config."""
        config_file = os.path.join(self.directory, '.config')
        if os.path.exists(config_file):
            try:
                with open(config_file, 'r') as f:
                    for line in f:
                        if line.startswith('CONFIG_ARCH_DEFCONFIG='):
                            # Format: CONFIG_ARCH_DEFCONFIG="arch/arm64/defconfig"
                            val = line.split('=', 1)[1].strip().strip('"')
                            if '/' in val:
                                return val.split('/')[1]
                        elif line.startswith('CONFIG_ARM64='):
                            return 'arm64'
                        elif line.startswith('CONFIG_X86_64=') or line.startswith('CONFIG_X86='):
                            return 'x86'
                        elif line.startswith('CONFIG_RISCV='):
                            return 'riscv'
            except IOError:
                pass
        # Try to detect from directory structure
        for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
            if os.path.exists(os.path.join(self.directory, f'arch/{arch}')):
                return arch
        return 'arm64'  # Default fallback

    def _check_config(self, option):
        """Check if a config option is enabled."""
        config_file = os.path.join(self.directory, 'include', 'config', 'auto.conf')
        if not os.path.exists(config_file):
            # Try .config
            config_file = os.path.join(self.directory, '.config')
        if not os.path.exists(config_file):
            return False
        try:
            with open(config_file, 'r') as f:
                for line in f:
                    if line.strip() == f"{option}=y":
                        return True
        except IOError:
            pass
        return False

    def _get_kbuild_ldflags(self):
        """Get KBUILD_LDFLAGS from make."""
        try:
            result = subprocess.run(
                ['make', '-p', '-f', 'Makefile', 'vmlinux'],
                cwd=self.directory,
                capture_output=True,
                text=True,
                timeout=30
            )
            for line in result.stdout.split('\n'):
                if line.startswith('KBUILD_LDFLAGS = '):
                    return line.split('=', 1)[1].strip()
        except Exception:
            pass
        # Default flags for arm64
        return '-EL -maarch64elf -z noexecstack --no-warn-rwx-segments'

    def _get_kbuild_lds(self):
        """Get KBUILD_LDS path."""
        # First check if the linker script exists
        for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
            lds = os.path.join(self.directory, f'arch/{arch}/kernel/vmlinux.lds')
            if os.path.exists(lds):
                return lds
        return None

    def _find_linker_script(self):
        """Find the linker script for the architecture."""
        # Try common locations (both .lds and .lds.S)
        for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
            # Check for preprocessed .lds file first
            lds = os.path.join(self.directory, f'arch/{arch}/kernel/vmlinux.lds')
            if os.path.exists(lds):
                return lds
            # Check for .lds.S source file
            lds_s = os.path.join(self.directory, f'arch/{arch}/kernel/vmlinux.lds.S')
            if os.path.exists(lds_s):
                return lds_s
        return None

    def _get_ld_cmd(self, arch):
        """Get the linker command for the architecture."""
        # Try to detect from .config or environment
        ld_cmds = {
            'arm64': '/usr/bin/aarch64-linux-gnu-ld.bfd',
            'x86_64': 'ld',
            'x86': 'ld',
            'riscv': '/usr/bin/riscv64-linux-gnu-ld.bfd',
            'arm': '/usr/bin/arm-linux-gnueabihf-ld.bfd',
            'mips': '/usr/bin/mips-linux-gnu-ld.bfd',
            'powerpc': '/usr/bin/powerpc-linux-gnu-ld.bfd',
        }
        return ld_cmds.get(arch, 'ld')

    def _get_kbuild_ldflags(self, arch):
        """Get KBUILD_LDFLAGS for the architecture."""
        # Architecture-specific linker flags
        flags = {
            'arm64': '-EL -maarch64elf -z noexecstack --no-warn-rwx-segments',
            'x86_64': '-m elf_x86_64 -z noexecstack',
            'x86': '-m elf_i386 -z noexecstack',
            'riscv': '-m elf64lriscv -z noexecstack',
            'arm': '-EL -m armelf_linux_eabi -z noexecstack',
            'mips': '-EL -m elf64ltsmip -z noexecstack',
            'powerpc': '-m elf64ppc -z noexecstack',
        }
        return flags.get(arch, '-z noexecstack')

    def _get_linuxinclude(self, srcarch):
        """Get LINUXINCLUDE flags for the architecture."""
        # Build the include paths dynamically based on architecture
        includes = [
            f'-I./arch/{srcarch}/include',
            f'-I./arch/{srcarch}/include/generated',
            '-I./include',
            f'-I./arch/{srcarch}/include/uapi',
            f'-I./arch/{srcarch}/include/generated/uapi',
            '-I./include/uapi',
            '-I./include/generated/uapi',
            '-include ./include/linux/compiler-version.h',
            '-include ./include/linux/kconfig.h',
        ]
        return ' '.join(includes)

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