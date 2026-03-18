# KBUILD Ninja Build System - Technical Reference

A toolset for integrating the Linux kernel build system with the Ninja build system. Generates `build.ninja` files by parsing kernel `.cmd` files, enabling fast incremental builds.

## Quick Start

```bash
# In-tree build
make allnoconfig && make ninja && ninja

# Out-of-tree build
make O=out allnoconfig && make O=out prepare && make O=out ninja && ninja -C out

# Generate .cmd files only
make cmdfiles
```

---

## Code-Level Technical Details

### Regular Expression Definitions

```python
# Match .cmd filenames (e.g., .slub.o.cmd)
_FILENAME_PATTERN = r'^\..*\.cmd$'

# Parse .cmd file content: savedcmd_<target> := <command>
_CMD_PATTERN = r'^savedcmd_([^ ]+) := (.+)$'

# Extract source file path from compile command
_COMPILE_PATTERN = r'^(.* )(?P<file_path>[^ ]*\.[cS])$'

# Detect objcopy command (.o -> .pi.o transformation)
_OBJCOPY_PATTERN = r'objcopy\s+.*\s+(\S+\.o)\s+(\S+\.pi\.o)'
```

### NinjaFileGenerator Class Data Structures

```python
class NinjaFileGenerator:
    def __init__(self, directory, output, ar, ld, cc):
        self.directory = directory   # Build directory
        self.output = output         # Output file path
        self.ar = ar                 # Archive tool command
        self.ld = ld                 # Linker command
        self.cc = cc                 # Compiler command
        
        # Core data structures
        self.compile_rules = {}      # obj -> (source, command)
        self.objcopy_rules = {}      # target.o -> (input_o, command)
        self.archive_rules = {}      # archive -> [objs]
        self.vmlinux_a_deps = []     # built-in.a files for vmlinux.a
        self.vmlinux_libs = []       # lib.a files
        self.modules = []            # .ko module list
        self.special_objs = {}       # Special objects: export, builtin_dtbs
```

### Constants

```python
_DEFAULT_OUTPUT = 'build.ninja'
_DEFAULT_LOG_LEVEL = 'WARNING'
_EXCLUDE_DIRS = ['.git', 'Documentation', 'include', 'tools', 'out', 'o']
```

---

## Path Processing Functions

### escape_ninja_path()

```python
def escape_ninja_path(path):
    """Escape special characters in Ninja paths"""
    if path is None:
        return ''
    # $ -> $$, space -> $ , : -> $:
    return path.replace('$', '$$').replace(' ', '$ ').replace(':', '$:')
```

### escape_ninja_cmd()

```python
def escape_ninja_cmd(cmd):
    """Escape $ in Ninja commands, preserving $(...) shell substitution"""
    if cmd is None:
        return ''
    result = []
    i = 0
    while i < len(cmd):
        if cmd[i] == '$':
            if i + 1 < len(cmd) and cmd[i + 1] == '(':
                # Keep $( as shell command substitution
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
```

### fix_depfile_path()

```python
def fix_depfile_path(command, target):
    """
    Fix depfile path format for Ninja compatibility.
    
    Kernel format: -Wp,-MMD,mm/.slub.o.d (hidden file format)
    Ninja format:  -Wp,-MMD,mm/slub.o.d (same name as output)
    """
    pattern = r'-Wp,-MMD,([^,]+)/\.([^,]+)\.d'
    
    def replace_depfile(m):
        dir_path = m.group(1)
        name = m.group(2)
        return f'-Wp,-MMD,{dir_path}/{name}.d'
    
    return re.sub(pattern, replace_depfile, command)
```

### normalize_path()

```python
def normalize_path(path):
    """Normalize path by removing double slashes"""
    while '//' in path:
        path = path.replace('//', '/')
    return path
```

---

## Parsing Functions

### parse_cmdfile()

```python
def parse_cmdfile(cmdfile_path):
    """
    Parse .cmd file and extract target name and command.
    
    Returns:
        tuple: (target_name, command) or (None, None)
    
    Processing:
    - UTF-8 encoding with character replacement on errors
    - Replace $(pound) with #
    - Fix depfile path
    """
    try:
        with open(cmdfile_path, 'r', encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                match = re.match(_CMD_PATTERN, line)
                if match:
                    target = normalize_path(match.group(1))
                    command = match.group(2)
                    command = command.replace('$(pound)', '#')
                    command = fix_depfile_path(command, target)
                    return target, command
    except (IOError, OSError):
        pass
    return None, None
```

### parse_archive_for_objs()

```python
def parse_archive_for_objs(archive_path, ar_cmd):
    """Parse archive file and return list of contained object files"""
    try:
        output = subprocess.check_output(
            [ar_cmd, '-t', archive_path],
            stderr=subprocess.DEVNULL
        )
        objs = output.decode().strip().split()
        archive_dir = os.path.dirname(archive_path)
        return [os.path.join(archive_dir, obj) for obj in objs if obj]
    except (subprocess.CalledProcessError, OSError):
        return []
```

### cmdfiles_in_dir()

```python
def cmdfiles_in_dir(directory):
    """Generate iterator of all .cmd files in directory"""
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
```

---

## Kernel Patch Details

### Makefile Modifications

**New help output:**
```
ninja           - Generate build.ninja for the ninja build system
```

**New ninja target:**
```makefile
quiet_cmd_gen_ninja = GEN     build.ninja
      cmd_gen_ninja = $(PYTHON3) $< -d $(objtree) -a $(AR) --ld $(LD) --cc $(CC) -o build.ninja $(objtree) $(if $(CONFIG_MODULES),modules.order)

ninja: $(srctree)/scripts/generate_ninja.py FORCE
	$(Q)$(MAKE) cmdfiles
	$(call cmd,gen_ninja)

targets += build.ninja
```

**Variable reference:**
| Variable | Meaning |
|----------|---------|
| `$(PYTHON3)` | Python 3 interpreter path |
| `$(objtree)` | Object tree directory (build output directory) |
| `$(srctree)` | Source tree directory |
| `$(AR)` | Archive tool (default: ar) |
| `$(LD)` | Linker (default: ld) |
| `$(CC)` | Compiler (default: gcc) |
| `$(build)` | Kbuild function, equals `-f $(srctree)/scripts/Makefile.build` |
| `$(if $(CONFIG_MODULES),modules.order)` | Pass modules.order if modules enabled |

**New cmdfiles target:**
```makefile
PHONY += cmdfiles

cmdfiles:
	$(Q)$(MAKE) $(build)=. need-builtin=1 need-modorder=1 cmdfiles-mode=1 cmdfiles
```

**Parameter reference:**
| Parameter | Value | Purpose |
|-----------|-------|---------|
| `$(build)=.` | -f scripts/Makefile.build obj=. | Start build from root |
| `need-builtin=1` | - | Generate built-in.a.cmd |
| `need-modorder=1` | - | Process modules.order |
| `cmdfiles-mode=1` | - | Only generate .cmd files |

### scripts/Kbuild.include Modifications

```makefile
# Generate .cmd file only, without executing command
# Usage: $(call savecmd_only,cmdname)
savecmd_only = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(make-cmd)' > $(dot-target).cmd 2>/dev/null || \
	printf '%s\n' 'savedcmd_$@ := $(make-cmd)' > $(dot-target).cmd

cmd_save_only = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(make-cmd)' > $(dot-target).cmd 2>/dev/null || \
	printf '%s\n' 'savedcmd_$@ := $(make-cmd)' > $(dot-target).cmd
```

**Variable reference:**
| Variable | Meaning |
|----------|---------|
| `$(make-cmd)` | Complete make command string |
| `$(dot-target)` | Target file prefix, e.g., `mm/.slub.o` |
| `$(objtree)/scripts/basic/fixdep` | Dependency processing tool |

**fixdep tool functions:**
1. Parse dependency files (.d files)
2. Generate .cmd files containing complete command and dependencies
3. Fallback to printf direct output if fixdep fails

### scripts/Makefile.build Modifications

**Rules to skip (cmdfiles-mode):**
```makefile
# Skip builtin targets
ifndef cmdfiles-mode
ifneq ($(strip $(lib-y) $(lib-m) $(lib-)),)
targets-for-builtin += $(obj)/lib.a
endif
ifdef need-builtin
targets-for-builtin += $(obj)/built-in.a
endif
endif

# Skip compile rules
ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.c $(recordmcount_source) FORCE
	$(call if_changed_rule,cc_o_c)
	$(call cmd,force_checksrc)
endif

# Skip Rust compilation
ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.rs FORCE
	+$(call if_changed_rule,rustc_o_rs)
endif

# Skip assembly compilation
ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.S FORCE
	$(call if_changed_rule,as_o_S)
endif

# Skip archive rules
ifndef cmdfiles-mode
$(obj)/built-in.a: $(real-obj-y) FORCE
	$(call if_changed,ar_builtin)
endif

ifndef cmdfiles-mode
$(obj)/lib.a: $(lib-y) FORCE
	$(call if_changed,ar)
endif

# Skip directory build
ifndef cmdfiles-mode
$(obj)/: $(if $(KBUILD_BUILTIN), $(targets-for-builtin)) \
	 $(if $(KBUILD_MODULES), $(targets-for-modules)) \
	 $(subdir-ym) $(always-y)
	@:
endif
```

**cmdfiles-specific rules:**
```makefile
PHONY += cmdfiles

# Compile command templates
cmd_cmdfiles_c = $(CC) $(c_flags) -c -o $@ $<
cmd_cmdfiles_S = $(CC) $(a_flags) -D__ASSEMBLY__ -c -o $@ $<

# Generate .cmd file (silent)
quiet_cmd_savecmd_c =
      cmd_savecmd_c = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(call make-cmd,cmdfiles_c)' > $(dot-target).cmd 2>/dev/null || \
	printf '%s\n' 'savedcmd_$@ := $(call make-cmd,cmdfiles_c)' > $(dot-target).cmd

quiet_cmd_savecmd_S =
      cmd_savecmd_S = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(call make-cmd,cmdfiles_S)' > $(dot-target).cmd 2>/dev/null || \
	printf '%s\n' 'savedcmd_$@ := $(call make-cmd,cmdfiles_S)' > $(dot-target).cmd

# Pattern rules (only enabled when cmdfiles-mode=1)
ifdef cmdfiles-mode
$(obj)/%.o: $(src)/%.c FORCE
	$(Q)mkdir -p $(obj) 2>/dev/null
	$(call cmd,savecmd_c)

$(obj)/%.o: $(src)/%.S FORCE
	$(Q)mkdir -p $(obj) 2>/dev/null
	$(call cmd,savecmd_S)
endif
```

**Source file collection variables:**
```makefile
# Collect all .o files to process
cmdfiles-all-objs := $(sort $(real-obj-y) $(lib-y) $(targets))

# Extract existing C source files
cmdfiles-c-srcs := $(wildcard $(patsubst $(obj)/%.o,$(src)/%.c,$(cmdfiles-all-objs)))

# Extract existing assembly source files
cmdfiles-S-srcs := $(wildcard $(patsubst $(obj)/%.o,$(src)/%.S,$(cmdfiles-all-objs)))

# Target object list
cmdfiles-objs := $(patsubst $(src)/%.c,$(obj)/%.o,$(cmdfiles-c-srcs)) \
                 $(patsubst $(src)/%.S,$(obj)/%.o,$(cmdfiles-S-srcs))
```

**Complete cmdfiles target implementation:**
```makefile
cmdfiles: $(cmdfiles-objs)
	$(Q)objdir="$(obj)"; objdir=$${objdir%/}; \
	if [ "$(need-builtin)" = "1" ]; then \
		printf 'savedcmd_%s/built-in.a := rm -f %s/built-in.a && %s cDPrST %s/built-in.a %s\n' \
			"$$objdir" "$$objdir" "$(AR)" "$$objdir" "$(sort $(real-obj-y:$(obj)/%=%))" \
			> "$$objdir/.built-in.a.cmd"; \
	fi; \
	if [ -n "$(strip $(lib-y))" ]; then \
		printf 'savedcmd_%s/lib.a := rm -f %s/lib.a && %s cDPrST %s/lib.a %s\n' \
			"$$objdir" "$$objdir" "$(AR)" "$$objdir" "$(sort $(lib-y:$(obj)/%=%))" \
			> "$$objdir/.lib.a.cmd"; \
	fi; \
	for dir in $(subdir-ym); do \
		need_builtin=0; \
		case " $(subdir-builtin) " in \
			*" $$dir/built-in.a "*) need_builtin=1 ;; \
		esac; \
		need_modorder=0; \
		case " $(subdir-modorder) " in \
			*" $$dir/modules.order "*) need_modorder=1 ;; \
		esac; \
		$(MAKE) -f $(srctree)/scripts/Makefile.build \
			srctree=$(srctree) \
			objtree=$(objtree) \
			srcroot=$(srcroot) \
			obj=$$dir \
			need-builtin=$$need_builtin \
			need-modorder=$$need_modorder \
			cmdfiles-mode=1 \
			cmdfiles; \
	done
```

**Variable reference:**
| Variable | Meaning |
|----------|---------|
| `$(subdir-ym)` | List of subdirectories to recurse into |
| `$(subdir-builtin)` | List of subdirs containing built-in.a |
| `$(subdir-modorder)` | List of subdirs containing modules.order |
| `$(real-obj-y)` | Actual object files to compile |
| `$(lib-y)` | Library object files |

**Debug target:**
```makefile
print-subdirs:
	@echo "subdir-ym: $(subdir-ym)"
	@echo "subdir-builtin: $(subdir-builtin)"
	@echo "obj-y: $(obj-y)"
	@echo "real-obj-y: $(real-obj-y)"
```

### arch/arm64/kernel/Makefile Modification

```makefile
# Force dependency (vdso*-wrap.S includes vdso.so through incbin)
# In cmdfiles mode, skip this dependency since we only generate .cmd files
ifndef cmdfiles-mode
$(obj)/vdso-wrap.o: $(obj)/vdso/vdso.so
$(obj)/vdso32-wrap.o: $(obj)/vdso32/vdso.so
endif
```

---

## generate_cmdfiles.py Details

### Command Line Arguments

```
usage: generate_cmdfiles.py [-h] [--srcarch SRCARCH] [--srctree SRCTREE]
                            [--objtree OBJTREE] [-j JOBS] [-v]

options:
  --srcarch        Source architecture (default: $SRCARCH or arm64)
  --srctree        Source tree root directory (default: $srctree or cwd)
  --objtree        Object tree root directory (default: $objtree or cwd)
  -j, --jobs       Number of parallel jobs (default: 4)
  -v, --verbose    Enable verbose output
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `$SRCARCH` | Source architecture | arm64 |
| `$srctree` | Source tree directory | current directory |
| `$objtree` | Object tree directory | current directory |

### Directories Processed

```python
def get_directories(srcarch):
    return [
        'init', 'drivers', 'kernel', 'lib', 'mm', 'net', 'fs',
        'ipc', 'security', 'crypto', 'block', 'io_uring', 'virt',
        f'arch/{srcarch}',
    ]
```

### Parallel Processing Implementation

```python
from concurrent.futures import ThreadPoolExecutor, as_completed

# Timeout per directory
TIMEOUT_PER_DIR = 300  # 5 minutes

def generate_for_dir(dir_path, srctree, objtree, verbose=False):
    """Process a single directory"""
    makefile_build = os.path.join(srctree, 'scripts', 'Makefile.build')
    
    cmd = [
        'make', '-f', makefile_build,
        f'srcroot={srctree}',
        f'srctree={srctree}',
        f'objtree={objtree}',
        f'obj={dir_path}',
        'cmdfiles-mode=1',
        'cmdfiles'
    ]
    
    try:
        result = subprocess.run(
            cmd, cwd=objtree, capture_output=True, text=True,
            timeout=TIMEOUT_PER_DIR
        )
        if result.returncode != 0 and verbose:
            return (dir_path, False, f"failed: {result.stderr[:200]}")
        return (dir_path, True, "done")
    except subprocess.TimeoutExpired:
        return (dir_path, False, "timeout")
    except Exception as e:
        return (dir_path, False, str(e))

# Main loop
with ThreadPoolExecutor(max_workers=args.jobs) as executor:
    futures = {
        executor.submit(generate_for_dir, d, srctree, objtree, verbose): d
        for d in existing_dirs
    }
    for future in as_completed(futures):
        dir_path, success, message = future.result()
        # Process result...
```

### Return Value Convention

```python
return 0 if fail_count == 0 else 1
```

---

## generate_ninja.py Details

### Command Line Arguments

```
usage: generate_ninja.py [-h] [-d DIRECTORY] [-o OUTPUT] [--log_level LEVEL]
                         [-a AR] [--ld LD] [--cc CC] [paths ...]

positional arguments:
  paths              Directories to search or files to parse

options:
  -d, --directory    Kernel build output directory (default: .)
  -o, --output       Output ninja file path (default: build.ninja)
  --log_level        Log level: DEBUG/INFO/WARNING/ERROR/CRITICAL (default: WARNING)
  -a, --ar           Archive tool command (default: ar)
  --ld               Linker command (default: ld)
  --cc               Compiler command (default: gcc)
```

### _parse_ar_command()

```python
def _parse_ar_command(self, command, archive):
    """Parse ar command to extract object dependencies"""
    parts = command.split()
    objs = []
    found_archive = False
    archive_basename = os.path.basename(archive)
    archive_dir = os.path.dirname(archive)
    
    for part in parts:
        part = normalize_path(part)
        
        if part == archive or part == archive_basename:
            found_archive = True
            continue
        
        if found_archive and (part.endswith('.o') or part.endswith('.a')):
            if os.path.isabs(part):
                obj_path = part
            elif archive_dir and archive_dir != '.':
                obj_path = os.path.join(archive_dir, part)
            else:
                obj_path = part
            objs.append(normalize_path(obj_path))
    
    return objs
```

### collect_vmlinux_deps()

```python
def collect_vmlinux_deps(self):
    """Collect vmlinux dependencies from built-in.a.cmd"""
    # Parse root built-in.a.cmd
    root_builtin_cmd = os.path.join(self.directory, '.built-in.a.cmd')
    if os.path.exists(root_builtin_cmd):
        target, command = parse_cmdfile(root_builtin_cmd)
        if target and command:
            deps = self._parse_ar_command(command, target)
            # built-in.a files as vmlinux.a dependencies
            self.vmlinux_a_deps = [d for d in deps if d.endswith('built-in.a')]
            # lib.a files processed separately
            self.vmlinux_libs = [d for d in deps if d.endswith('lib.a')]
    
    # Check for existing vmlinux.a
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
```

### _synthesize_missing_objcopy_rules()

```python
def _synthesize_missing_objcopy_rules(self):
    """Synthesize objcopy rules for .pi.o files missing .cmd files"""
    # Out-of-tree detection
    dir_abs = os.path.realpath(self.directory)
    cwd_abs = os.path.realpath(os.getcwd())
    same_dir = dir_abs == cwd_abs
    
    has_dotdot_prefix = any(
        source and source.startswith('../')
        for obj, (source, _) in self.compile_rules.items()
    )
    
    out_of_tree = not same_dir or has_dotdot_prefix
    prefix = '../' if out_of_tree else ''
    
    # Find .pi.o files needing synthesized rules
    for archive, objs in self.archive_rules.items():
        for obj in objs:
            if obj.endswith('.pi.o') and obj not in self.objcopy_rules:
                # .pi.o -> .o (remove 5 chars and add .o)
                base_o = obj[:-5] + '.o'
                
                # Synthesize compile rule for lib-*.o
                if base_o not in self.compile_rules and os.path.basename(base_o).startswith('lib-'):
                    lib_name = os.path.basename(base_o)[4:-2]  # remove 'lib-' and '.o'
                    src_path = f'{prefix}lib/{lib_name}.c'
                    
                    # Use existing kernel/pi/ rule as template
                    pi_compile_rules = [
                        (t, (s, c)) for t, (s, c) in self.compile_rules.items()
                        if 'kernel/pi/' in t and t.endswith('.o') and not t.endswith('.pi.o')
                    ]
                    if pi_compile_rules:
                        sample_target, (sample_src, sample_cmd) = pi_compile_rules[0]
                        synthesized_compile_cmd = sample_cmd.replace(sample_target, base_o)
                        synthesized_compile_cmd = re.sub(
                            r'(\.\./)?arch/arm64/kernel/pi/\S+\.c',
                            src_path, synthesized_compile_cmd
                        )
                        self.compile_rules[base_o] = (src_path, synthesized_compile_cmd)
                
                # Synthesize objcopy rule
                if base_o in self.compile_rules:
                    if self.objcopy_rules:
                        sample_target = next(iter(self.objcopy_rules.keys()))
                        _, sample_cmd = self.objcopy_rules[sample_target]
                        sample_base = sample_target[:-5] + '.o'
                        synthesized_cmd = sample_cmd.replace(sample_base, base_o).replace(sample_target, obj)
                    else:
                        synthesized_cmd = f'objcopy --prefix-symbols=__pi_ --remove-section=.note.gnu.property {base_o} {obj}'
                    self.objcopy_rules[obj] = (base_o, synthesized_cmd)
```

### _synthesize_missing_dtb_rules()

```python
def _synthesize_missing_dtb_rules(self):
    """Handle missing DTB and generated files"""
    for archive, objs in list(self.archive_rules.items()):
        for obj in list(objs):
            if obj.endswith('.o') and obj not in self.compile_rules and obj not in self.objcopy_rules:
                if obj.endswith('.dtb.o'):
                    # Device tree files - remove from dependencies
                    self.archive_rules[archive].remove(obj)
                elif 'deftbl' in obj or 'defkeymap' in obj:
                    # Generated source files - remove from dependencies
                    self.archive_rules[archive].remove(obj)
                else:
                    # Try to find source file
                    obj_dir = os.path.dirname(obj)
                    obj_name = os.path.basename(obj).replace('.o', '')
                    prefix = '../' if self.directory != '.' else ''
                    
                    for ext in ['.c', '.S']:
                        src_path = os.path.join(prefix, obj_dir, obj_name + ext) if prefix else os.path.join(obj_dir, obj_name + ext)
                        if os.path.exists(src_path):
                            if self.compile_rules:
                                sample_target = next(iter(self.compile_rules.keys()))
                                _, sample_cmd = self.compile_rules[sample_target]
                                synthesized_cmd = sample_cmd.replace(sample_target, obj)
                                synthesized_cmd = re.sub(r'\S+\.c$', src_path, synthesized_cmd)
                                self.compile_rules[obj] = (src_path, synthesized_cmd)
                            break
```

### _get_kbuild_ldflags()

```python
def _get_kbuild_ldflags(self):
    """Get KBUILD_LDFLAGS from make"""
    try:
        result = subprocess.run(
            ['make', '-p', '-f', 'Makefile', 'vmlinux'],
            cwd=self.directory, capture_output=True, text=True,
            timeout=30  # 30 second timeout
        )
        for line in result.stdout.split('\n'):
            if line.startswith('KBUILD_LDFLAGS = '):
                return line.split('=', 1)[1].strip()
    except Exception:
        pass
    # arm64 default flags
    return '-EL -maarch64elf -z noexecstack --no-warn-rwx-segments'
```

### _find_linker_script()

```python
def _find_linker_script(self):
    """Find architecture linker script"""
    # Search by priority
    for arch in ['arm64', 'x86', 'riscv', 'arm', 'mips', 'powerpc']:
        lds = os.path.join(self.directory, f'arch/{arch}/kernel/vmlinux.lds')
        if os.path.exists(lds):
            return lds
    return None
```

### _check_config()

```python
def _check_config(self, option):
    """Check if a config option is enabled"""
    # Prefer auto.conf
    config_file = os.path.join(self.directory, 'include', 'config', 'auto.conf')
    if not os.path.exists(config_file):
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
```

### Output Statistics

```python
print(f'Done. Generated {output}')
print(f'  - {len(generator.compile_rules)} compile rules')
print(f'  - {len(generator.objcopy_rules)} objcopy rules')
print(f'  - {len(generator.archive_rules)} archive rules')
print(f'  - {len(generator.modules)} modules')
```

---

## link_vmlinux_ninja.py Details

### Complete Implementation

```python
#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0

import argparse
import os
import subprocess
import sys

def parse_arguments():
    parser = argparse.ArgumentParser(description='Link vmlinux for ninja builds')
    parser.add_argument('--vmlinux-o', required=True, help='Path to vmlinux.o (input)')
    parser.add_argument('--output', '-o', required=True, help='Output vmlinux path')
    parser.add_argument('--objtree', default='.', help='Object tree root')
    parser.add_argument('--srctree', default='.', help='Source tree root')
    parser.add_argument('-v', '--verbose', action='store_true', help='Verbose output')
    return parser.parse_args()

def main():
    args = parse_arguments()
    
    objtree = os.path.realpath(args.objtree)
    srctree = os.path.realpath(args.srctree)
    
    # Build command
    if objtree == srctree:
        # In-tree build
        cmd = ['make', '-C', objtree, 'vmlinux']
    else:
        # Out-of-tree build
        cmd = ['make', '-C', objtree, f'SRCTREE={srctree}', 'vmlinux']
    
    # Run make vmlinux
    # kallsyms iteration, BTF generation, System.map handled by make
    result = subprocess.run(
        cmd, cwd=objtree,
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
    
    return 0

if __name__ == '__main__':
    sys.exit(main())
```

---

## Generated Ninja Rules Reference

### cc Rule

```ninja
rule cc
  command = $cmd
  description = CC $out
  deps = gcc
  depfile = $out.d
```

**Notes:**
- `deps = gcc` - Use GCC format for dependency parsing
- `depfile = $out.d` - Dependency file path

### ar Rule

```ninja
rule ar
  command = rm -f $out && $AR rcST $out $in
  description = AR $out
```

**AR flags reference:**
| Flag | Meaning |
|------|---------|
| `r` | Replace/add members |
| `c` | Create archive (no warning) |
| `S` | Don't generate symbol table (handled by linker) |
| `T` | Thin archive (don't duplicate storage) |

### ld_vmlinux_o Rule

```ninja
rule ld_vmlinux_o
  command = $LD -r -o $out --whole-archive $in --no-whole-archive --start-group $libs --end-group
  description = LD $out
```

**Linker flags reference:**
| Flag | Meaning |
|------|---------|
| `-r` | Generate relocatable output |
| `--whole-archive` | Include all objects in archive |
| `--no-whole-archive` | End whole-archive scope |
| `--start-group` | Begin circular dependency group |
| `--end-group` | End circular dependency group |

### objcopy Rule Example

```ninja
build arch/arm64/kernel/pi/idreg-override.pi.o: objcopy arch/arm64/kernel/pi/idreg-override.o
  cmd = objcopy --prefix-symbols=__pi_ --remove-section=.note.gnu.property arch/arm64/kernel/pi/idreg-override.o arch/arm64/kernel/pi/idreg-override.pi.o
```

**objcopy flags reference:**
| Flag | Meaning |
|------|---------|
| `--prefix-symbols=__pi_` | Add `__pi_` prefix to symbols |
| `--remove-section=.note.gnu.property` | Remove GNU property notes section |

---

## Special File Handling

### .pi.o Files

Position-independent code object files, converted from `.o` files via objcopy:

```
foo.o → objcopy --prefix-symbols=__pi_ → foo.pi.o
```

**Usage:** ARM64 kernel position-independent code running during early boot.

### .vmlinux.export.o

Kernel exported symbol table object file.

### .builtin-dtbs.o

Built-in device tree binary object file.

### .dtb.o

Device tree binary object files. Currently removed from dependencies (requires dtc tool).

### deftbl / defkeymap

Generated source files. Currently removed from dependencies.

---

## Build Flow Diagram

```
make ninja
    │
    ├─→ make cmdfiles (cmdfiles-mode=1)
    │       │
    │       ├─→ $(obj)/%.o: $(src)/%.c → generate .o.cmd
    │       │
    │       ├─→ $(obj)/%.o: $(src)/%.S → generate .o.cmd
    │       │
    │       ├─→ printf ... > .built-in.a.cmd
    │       │
    │       ├─→ printf ... > .lib.a.cmd
    │       │
    │       └─→ $(MAKE) -f Makefile.build obj=subdir → recurse
    │
    └─→ python3 scripts/generate_ninja.py
            │
            ├─→ collect_cmdfiles() → parse all .cmd files
            │
            ├─→ collect_vmlinux_deps() → parse built-in.a.cmd
            │
            ├─→ collect_modules() → parse modules.order
            │
            └─→ generate() → write build.ninja
                    │
                    ├─→ _write_header()
                    ├─→ _write_rules()
                    ├─→ _write_compile_rules()
                    ├─→ _write_objcopy_rules()
                    ├─→ _write_archive_rules()
                    ├─→ _write_vmlinux_rules()
                    ├─→ _write_modules_rules()
                    └─→ _write_default_target()
```

---

## Error Handling

### File Reading

```python
try:
    with open(cmdfile_path, 'r', encoding='utf-8', errors='replace') as f:
        # Process file...
except (IOError, OSError):
    pass  # Silently ignore
```

### Subprocess

```python
try:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
except subprocess.TimeoutExpired:
    # Handle timeout
except subprocess.CalledProcessError:
    # Command failed
except OSError:
    # Command not found
```

---

## License

SPDX-License-Identifier: GPL-2.0
