#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0
#
# Link vmlinux for ninja builds
# This script replaces link-vmlinux.sh for ninja-based builds
#
# Features:
# - No make calls (pure Python implementation)
# - Kallsyms iteration driven by System.map comparison
# - BTF generation support
# - System.map generation

import argparse
import os
import subprocess
import sys
import tempfile
import shutil
from pathlib import Path


def parse_arguments():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Link vmlinux for ninja builds'
    )
    parser.add_argument(
        '--vmlinux-a', required=True,
        help='Path to vmlinux.a (input)'
    )
    parser.add_argument(
        '--output', '-o', required=True,
        help='Output vmlinux path'
    )
    parser.add_argument(
        '--objtree', default='.',
        help='Object tree root'
    )
    parser.add_argument(
        '--srctree', default='.',
        help='Source tree root'
    )
    parser.add_argument(
        '--lds', default=None,
        help='Linker script path (optional, will auto-detect if not provided)'
    )
    parser.add_argument(
        '--libs', default='',
        help='Libraries to link (space-separated)'
    )
    parser.add_argument(
        '--version-timestamp-o', default='init/version-timestamp.o',
        help='Path to version-timestamp.o'
    )
    parser.add_argument(
        '-v', '--verbose', action='store_true',
        help='Verbose output'
    )

    args = parser.parse_args()
    
    # Auto-detect linker script if not provided
    if args.lds is None:
        for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
            lds_path = os.path.join(args.objtree, f'arch/{arch}/kernel/vmlinux.lds')
            if os.path.exists(lds_path):
                args.lds = lds_path
                break
            # Try .lds.S source file
            lds_s_path = os.path.join(args.objtree, f'arch/{arch}/kernel/vmlinux.lds.S')
            if os.path.exists(lds_s_path):
                # Use .lds.S as fallback (linker may handle it)
                args.lds = lds_s_path
                break
        
        if args.lds is None:
            print("Error: Could not find linker script", file=sys.stderr)
            sys.exit(1)
    
    return args


def read_config(objtree, option):
    """Read a config option from auto.conf."""
    config_path = os.path.join(objtree, 'include/config/auto.conf')
    if not os.path.exists(config_path):
        return False
    
    with open(config_path, 'r') as f:
        for line in f:
            if line.strip() == f'{option}=y':
                return True
    return False


def info(msg, target):
    """Print info message in kbuild format."""
    print(f'  {msg:<7} {target}')


def run_command(cmd, verbose=False, cwd=None):
    """Run a command and return (returncode, stdout, stderr)."""
    if verbose:
        print(f'  Running: {" ".join(cmd)}')
    
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=cwd
    )
    return result.returncode, result.stdout, result.stderr


def get_file_size(path):
    """Get file size in bytes."""
    return os.path.getsize(path)


def vmlinux_link(objtree, output, ld, ldflags, objs, libs, kallsymso='',
                 btf_vmlinux_bin_o='', arch_vmlinux_o='', verbose=False):
    """Link vmlinux with the given objects."""
    info('LD', output)

    # Determine linker settings
    wl = ''
    ldlibs = ''

    # Read SRCARCH from environment or Makefile
    srcarch = os.environ.get('SRCARCH', '')
    if not srcarch:
        # Try to detect from .config
        config_path = os.path.join(objtree, '.config')
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                for line in f:
                    if line.startswith('CONFIG_ARCH_'):
                        srcarch = line.split('=')[0].replace('CONFIG_ARCH_', '').lower()
                        break

    if srcarch == 'um':
        wl = '-Wl,'
        cc = os.environ.get('CC', 'gcc')
        ld = cc
        ldflags = os.environ.get('CFLAGS_vmlinux', '')
        ldlibs = '-lutil -lrt -lpthread'

    # Build command - use ld directly (not via gcc)
    cmd = [ld]

    # Add linker flags first (before -o)
    for flag in ldflags.split():
        if flag:
            cmd.append(flag)

    # Output file
    cmd.extend(['-o', output])

    # Whole archive section
    cmd.append(f'{wl}--whole-archive')
    for obj in objs:
        if obj and os.path.exists(obj):
            cmd.append(obj)
    cmd.append(f'{wl}--no-whole-archive')

    # Library groups
    cmd.append(f'{wl}--start-group')
    for lib in libs:
        if lib and os.path.exists(lib):
            cmd.append(lib)
    cmd.append(f'{wl}--end-group')

    # Additional objects (kallsyms, btf, arch-specific)
    if kallsymso and os.path.exists(kallsymso):
        cmd.append(kallsymso)
    if btf_vmlinux_bin_o and os.path.exists(btf_vmlinux_bin_o):
        cmd.append(btf_vmlinux_bin_o)
    if arch_vmlinux_o and os.path.exists(arch_vmlinux_o):
        cmd.append(arch_vmlinux_o)
    if ldlibs:
        for lib in ldlibs.split():
            cmd.append(lib)

    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)

    if returncode != 0:
        print(f'Error: Link failed: {stderr}', file=sys.stderr)
        return False

    return True


def kallsyms(objtree, srctree, input_file, output_prefix, verbose=False):
    """Generate kallsyms object file."""
    # Check if KALLSYMS_ALL is enabled
    kallsymopt = ''
    if read_config(objtree, 'CONFIG_KALLSYMS_ALL'):
        kallsymopt = '--all-symbols'

    syms_file = f'{input_file}.syms'
    kallsyms_s = f'{output_prefix}.S'
    kallsyms_o = f'{output_prefix}.o'

    # Check if input file is empty (dummy file for initial pass)
    input_path = os.path.join(objtree, input_file)
    syms_path = os.path.join(objtree, syms_file)
    if os.path.exists(input_path) and os.path.getsize(input_path) == 0:
        # Empty dummy file - skip nm, create empty symbol file
        info('KSYMS', kallsyms_s)
        with open(syms_path, 'w') as f:
            pass  # Create empty file
    else:
        # Generate symbol file from ELF
        info('NM', syms_file)
        nm = os.environ.get('NM', 'nm')
        cmd = [nm, '-n', input_file]
        returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)

        if returncode != 0:
            print(f'Error: nm failed: {stderr}', file=sys.stderr)
            return None

        # Apply mksysmap filter
        mksysmap_script = os.path.join(srctree, 'scripts/mksysmap')
        if os.path.exists(mksysmap_script):
            result = subprocess.run(
                ['sed', '-f', mksysmap_script],
                input=stdout,
                capture_output=True,
                text=True,
                cwd=objtree
            )
            syms_content = result.stdout
        else:
            syms_content = stdout

        with open(os.path.join(objtree, syms_file), 'w') as f:
            f.write(syms_content)
    
    # Generate kallsyms.S
    info('KSYMS', kallsyms_s)
    kallsyms_cmd = [os.path.join(srctree, 'scripts/kallsyms')]
    if kallsymopt:
        kallsyms_cmd.append(kallsymopt)
    kallsyms_cmd.append(syms_file)  # kallsyms takes input file as argument

    result = subprocess.run(
        kallsyms_cmd,
        capture_output=True,
        text=True,
        cwd=objtree
    )

    if result.returncode != 0:
        print(f'Error: kallsyms failed: {result.stderr}', file=sys.stderr)
        return None

    with open(os.path.join(objtree, kallsyms_s), 'w') as f:
        f.write(result.stdout)
    
    # Assemble kallsyms.o
    info('AS', kallsyms_o)
    cc = os.environ.get('CC', 'gcc')
    
    # Build include flags
    nostdinc_flags = os.environ.get('NOSTDINC_FLAGS', '')
    linuxinclude = os.environ.get('LINUXINCLUDE', f'-I{srctree}/include')
    kbuild_cppflags = os.environ.get('KBUILD_CPPFLAGS', '')
    kbuild_aflags = os.environ.get('KBUILD_AFLAGS', '')
    kbuild_aflags_kernel = os.environ.get('KBUILD_AFLAGS_KERNEL', '')
    
    cmd = [cc]
    for flag in nostdinc_flags.split():
        if flag:
            cmd.append(flag)
    for flag in linuxinclude.split():
        if flag:
            cmd.append(flag)
    for flag in kbuild_cppflags.split():
        if flag:
            cmd.append(flag)
    for flag in kbuild_aflags.split():
        if flag:
            cmd.append(flag)
    for flag in kbuild_aflags_kernel.split():
        if flag:
            cmd.append(flag)
    cmd.extend(['-c', '-o', kallsyms_o, kallsyms_s])
    
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: Assembling kallsyms failed: {stderr}', file=sys.stderr)
        return None
    
    return kallsyms_o


def gen_btf(objtree, srctree, vmlinux_file, verbose=False):
    """Generate BTF data."""
    btf_data = f'{vmlinux_file}.btf.o'
    info('BTF', btf_data)
    
    pahole = os.environ.get('PAHOLE', 'pahole')
    pahole_flags = os.environ.get('PAHOLE_FLAGS', '')
    objcopy = os.environ.get('OBJCOPY', 'objcopy')
    
    # Generate BTF
    cmd = [pahole]
    if pahole_flags:
        for flag in pahole_flags.split():
            cmd.append(flag)
    cmd.extend(['-J', vmlinux_file])
    
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: BTF generation failed: {stderr}', file=sys.stderr)
        return None
    
    # Create btf.o file
    cmd = [
        objcopy,
        '--only-section=.BTF',
        '--set-section-flags', '.BTF=alloc,readonly',
        '--strip-all',
        vmlinux_file,
        btf_data
    ]
    
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: objcopy for BTF failed: {stderr}', file=sys.stderr)
        return None
    
    # Change e_type to ET_REL
    if read_config(objtree, 'CONFIG_CPU_BIG_ENDIAN'):
        et_rel = b'\x00\x01'
    else:
        et_rel = b'\x01\x00'
    
    with open(os.path.join(objtree, btf_data), 'r+b') as f:
        f.seek(16)
        f.write(et_rel)
    
    return btf_data


def mksysmap(objtree, srctree, vmlinux_file, output_map, verbose=False):
    """Generate System.map."""
    info('NM', output_map)
    
    nm = os.environ.get('NM', 'nm')
    cmd = [nm, '-n', vmlinux_file]
    
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: nm failed: {stderr}', file=sys.stderr)
        return False
    
    # Apply mksysmap filter
    mksysmap_script = os.path.join(srctree, 'scripts/mksysmap')
    if os.path.exists(mksysmap_script):
        result = subprocess.run(
            ['sed', '-f', mksysmap_script],
            input=stdout,
            capture_output=True,
            text=True,
            cwd=objtree
        )
        map_content = result.stdout
    else:
        map_content = stdout
    
    with open(os.path.join(objtree, output_map), 'w') as f:
        f.write(map_content)
    
    return True


def compare_system_maps(objtree, map1, map2, verbose=False):
    """Compare two System.map files. Returns True if they are identical."""
    map1_path = os.path.join(objtree, map1)
    map2_path = os.path.join(objtree, map2)
    
    if not os.path.exists(map1_path) or not os.path.exists(map2_path):
        return False
    
    with open(map1_path, 'r') as f:
        content1 = f.read()
    with open(map2_path, 'r') as f:
        content2 = f.read()
    
    return content1 == content2


def sorttable(objtree, srctree, vmlinux_file, verbose=False):
    """Sort kernel tables."""
    info('SORTTAB', vmlinux_file)
    
    nm = os.environ.get('NM', 'nm')
    sorttable_bin = os.path.join(objtree, 'scripts/sorttable')
    
    if not os.path.exists(sorttable_bin):
        print(f'Error: sorttable not found: {sorttable_bin}', file=sys.stderr)
        return False
    
    # Generate nm output
    cmd = [nm, '-S', vmlinux_file]
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: nm failed: {stderr}', file=sys.stderr)
        return False
    
    # Write to temp file
    temp_nm = os.path.join(objtree, '.tmp_vmlinux.nm-sort')
    with open(temp_nm, 'w') as f:
        f.write(stdout)
    
    # Run sorttable
    cmd = [sorttable_bin, '-s', temp_nm, vmlinux_file]
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    # Cleanup
    if os.path.exists(temp_nm):
        os.remove(temp_nm)
    
    if returncode != 0:
        print(f'Error: sorttable failed: {stderr}', file=sys.stderr)
        return False
    
    return True


def resolve_btfids(objtree, srctree, vmlinux_file, verbose=False):
    """Resolve BTF IDs."""
    info('BTFIDS', vmlinux_file)
    
    resolve_btfids_bin = os.path.join(objtree, 'tools/bpf/resolve_btfids/resolve_btfids')
    
    if not os.path.exists(resolve_btfids_bin):
        print(f'Error: resolve_btfids not found: {resolve_btfids_bin}', file=sys.stderr)
        return False
    
    args = ''
    if read_config(objtree, 'CONFIG_WERROR'):
        args = '--fatal_warnings'
    
    cmd = [resolve_btfids_bin]
    if args:
        cmd.append(args)
    cmd.append(vmlinux_file)
    
    returncode, stdout, stderr = run_command(cmd, verbose, cwd=objtree)
    
    if returncode != 0:
        print(f'Error: resolve_btfids failed: {stderr}', file=sys.stderr)
        return False
    
    return True


def cleanup(objtree):
    """Clean up temporary files."""
    files_to_remove = [
        '.btf.*',
        '.tmp_vmlinux.nm-sort',
        '.tmp_vmlinux*.syms',
        '.tmp_vmlinux*.kallsyms.S',
        '.tmp_vmlinux*.kallsyms.o',
        'System.map',
        'vmlinux.map'
    ]
    
    import glob
    for pattern in files_to_remove:
        for f in glob.glob(os.path.join(objtree, pattern)):
            try:
                os.remove(f)
            except:
                pass


def main():
    args = parse_arguments()
    
    objtree = os.path.realpath(args.objtree)
    srctree = os.path.realpath(args.srctree)
    vmlinux_a = os.path.realpath(args.vmlinux_a)
    output = os.path.realpath(args.output)
    version_timestamp_o = os.path.realpath(args.version_timestamp_o)

    if args.verbose:
        print(f"Linking vmlinux:")
        print(f"  objtree: {objtree}")
        print(f"  srctree: {srctree}")
        print(f"  vmlinux.a: {vmlinux_a}")
        print(f"  version-timestamp.o: {version_timestamp_o}")
        print(f"  output: {output}")
    
    # Get linker and flags from environment (set by ninja rules)
    ld = os.environ.get('LD', 'ld')
    kbuild_ldflags = os.environ.get('KBUILD_LDFLAGS', '-EL -maarch64elf -z noexecstack --no-warn-rwx-segments')
    ldflags_vmlinux = os.environ.get('LDFLAGS_vmlinux', '--no-undefined -X --pic-veneer -shared -Bsymbolic -z notext --no-apply-dynamic-reloc --build-id=sha1 --orphan-handling=warn')
    
    # Read config options
    config_lto_clang = read_config(objtree, 'CONFIG_LTO_CLANG')
    config_x86_kernel_ibt = read_config(objtree, 'CONFIG_X86_KERNEL_IBT')
    config_generic_builtin_dtb = read_config(objtree, 'CONFIG_GENERIC_BUILTIN_DTB')
    config_modules = read_config(objtree, 'CONFIG_MODULES')
    config_kallsyms = read_config(objtree, 'CONFIG_KALLSYMS')
    config_debug_info_btf = read_config(objtree, 'CONFIG_DEBUG_INFO_BTF')
    config_arch_wants_pre_link = read_config(objtree, 'CONFIG_ARCH_WANTS_PRE_LINK_VMLINUX')
    config_vmlinux_map = read_config(objtree, 'CONFIG_VMLINUX_MAP')
    config_buildtime_table_sort = read_config(objtree, 'CONFIG_BUILDTIME_TABLE_SORT')
    
    # Determine objects - vmlinux_a is passed directly from ninja
    objs = [vmlinux_a]
    libs = args.libs.split() if args.libs else []
    
    if config_generic_builtin_dtb:
        objs.append(os.path.join(objtree, '.builtin-dtbs.o'))
    
    if config_modules:
        objs.append(os.path.join(objtree, '.vmlinux.export.o'))
    
    # Add version-timestamp.o
    objs.append(version_timestamp_o)
    
    # Filter out non-existent objects (like .vmlinux.export.o when modpost hasn't run)
    objs = [obj for obj in objs if os.path.exists(obj)]
    
    # Arch-specific object
    arch_vmlinux_o = ''
    if config_arch_wants_pre_link:
        srcarch = os.environ.get('SRCARCH', '')
        arch_vmlinux_o = os.path.join(objtree, f'arch/{srcarch}/tools/vmlinux.arch.o')
    
    # Build ldflags for final link
    ldflags_final = f'{kbuild_ldflags} {ldflags_vmlinux} --script={args.lds}'
    # Build ldflags for temp links (without LDFLAGS_vmlinux)
    ldflags_temp = f'{kbuild_ldflags} --script={args.lds}'
    
    btf_vmlinux_bin_o = ''
    kallsymso = ''
    
    # Initial dummy kallsyms if needed
    if config_kallsyms:
        dummy_syms = os.path.join(objtree, '.tmp_vmlinux0.syms')
        with open(dummy_syms, 'w') as f:
            pass
        kallsymso = kallsyms(objtree, srctree, dummy_syms, '.tmp_vmlinux0.kallsyms', args.verbose)

    # First link (for BTF and kallsyms sizing) - use ldflags_temp (without LDFLAGS_vmlinux)
    if config_kallsyms or config_debug_info_btf:
        strip_debug = not config_debug_info_btf
        tmp_vmlinux1 = os.path.join(objtree, '.tmp_vmlinux1')

        tmp_ldflags = ldflags_temp
        if strip_debug:
            tmp_ldflags += ' --strip-debug'
        if config_vmlinux_map:
            tmp_ldflags += ' -Map=vmlinux.map'

        if not vmlinux_link(objtree, tmp_vmlinux1, ld, tmp_ldflags, objs, libs,
                           kallsymso, btf_vmlinux_bin_o, arch_vmlinux_o, args.verbose):
            cleanup(objtree)
            return 1

        # Generate BTF if needed
        if config_debug_info_btf:
            btf_vmlinux_bin_o = gen_btf(objtree, srctree, tmp_vmlinux1, args.verbose)
            if not btf_vmlinux_bin_o:
                print("Failed to generate BTF for vmlinux", file=sys.stderr)
                print("Try to disable CONFIG_DEBUG_INFO_BTF", file=sys.stderr)
                cleanup(objtree)
                return 1

    # Kallsyms iteration - use ldflags_temp for temp links
    if config_kallsyms:
        strip_debug = True

        # First pass
        kallsymso = kallsyms(objtree, srctree, '.tmp_vmlinux1', '.tmp_vmlinux1.kallsyms', args.verbose)
        if not kallsymso:
            cleanup(objtree)
            return 1

        size1 = get_file_size(os.path.join(objtree, kallsymso))

        # Second link
        tmp_vmlinux2 = os.path.join(objtree, '.tmp_vmlinux2')
        tmp_ldflags = ldflags_temp + ' --strip-debug'
        if config_vmlinux_map:
            tmp_ldflags += ' -Map=vmlinux.map'

        if not vmlinux_link(objtree, tmp_vmlinux2, ld, tmp_ldflags, objs, libs,
                           kallsymso, btf_vmlinux_bin_o, arch_vmlinux_o, args.verbose):
            cleanup(objtree)
            return 1

        kallsymso = kallsyms(objtree, srctree, '.tmp_vmlinux2', '.tmp_vmlinux2.kallsyms', args.verbose)
        if not kallsymso:
            cleanup(objtree)
            return 1

        size2 = get_file_size(os.path.join(objtree, kallsymso))

        # Third pass if sizes differ or KALLSYMS_EXTRA_PASS is set
        if size1 != size2 or os.environ.get('KALLSYMS_EXTRA_PASS'):
            tmp_vmlinux3 = os.path.join(objtree, '.tmp_vmlinux3')
            if not vmlinux_link(objtree, tmp_vmlinux3, ld, tmp_ldflags, objs, libs,
                               kallsymso, btf_vmlinux_bin_o, arch_vmlinux_o, args.verbose):
                cleanup(objtree)
                return 1

            kallsymso = kallsyms(objtree, srctree, '.tmp_vmlinux3', '.tmp_vmlinux3.kallsyms', args.verbose)
            if not kallsymso:
                cleanup(objtree)
                return 1

    # Final link - use ldflags_final (with LDFLAGS_vmlinux)
    ldflags = ldflags_final
    if config_vmlinux_map:
        ldflags += ' -Map=vmlinux.map'

    if not vmlinux_link(objtree, output, ld, ldflags, objs, libs,
                       kallsymso, btf_vmlinux_bin_o, arch_vmlinux_o, args.verbose):
        cleanup(objtree)
        return 1
    
    # Resolve BTF IDs if needed
    if config_debug_info_btf:
        if not resolve_btfids(objtree, srctree, output, args.verbose):
            cleanup(objtree)
            return 1
    
    # Generate System.map
    if not mksysmap(objtree, srctree, output, 'System.map', args.verbose):
        cleanup(objtree)
        return 1
    
    # Sort tables if needed
    if config_buildtime_table_sort:
        if not sorttable(objtree, srctree, output, args.verbose):
            print("Failed to sort kernel tables", file=sys.stderr)
            cleanup(objtree)
            return 1
    
    # Verify kallsyms consistency
    if config_kallsyms:
        # Get the last kallsyms sysmap
        if size1 != size2:
            kallsyms_sysmap = '.tmp_vmlinux3.syms'
        else:
            kallsyms_sysmap = '.tmp_vmlinux2.syms'
        
        if not compare_system_maps(objtree, 'System.map', kallsyms_sysmap, args.verbose):
            print("Inconsistent kallsyms data", file=sys.stderr)
            print('Try "make KALLSYMS_EXTRA_PASS=1" as a workaround', file=sys.stderr)
            cleanup(objtree)
            return 1
    
    if args.verbose:
        print(f"vmlinux linked successfully: {output}")
    
    # Cleanup temp files (keep System.map)
    for pattern in ['.btf.*', '.tmp_vmlinux*.syms', '.tmp_vmlinux*.kallsyms.S', '.tmp_vmlinux*.kallsyms.o']:
        import glob
        for f in glob.glob(os.path.join(objtree, pattern)):
            try:
                os.remove(f)
            except:
                pass
    
    return 0


if __name__ == '__main__':
    sys.exit(main())
