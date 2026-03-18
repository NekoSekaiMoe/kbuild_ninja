# Linux Kernel Ninja Build Integration

This project enables the Linux kernel to use the [Ninja build system](https://ninja-build.org/) for faster incremental builds. It generates `build.ninja` files from kernel build artifacts without requiring a full compilation first.

## Overview

The integration works by:
1. Extracting build commands from kernel `.cmd` files (generated during configuration)
2. Translating them into Ninja build rules
3. Supporting full vmlinux linking, module builds, and incremental compilation

## Prerequisites

Before integrating, ensure you have:

- Python 3.8 or newer
- Ninja build system (`ninja` or `samu`)
- Standard kernel build toolchain (GCC/Clang, binutils, etc.)
- A configured kernel tree (`.config` must exist)

## Manual Integration (Without Patch)

Follow these steps to manually integrate the Ninja build system into your kernel source tree.

### Step 1: Copy Python Scripts

Copy the generator scripts to your kernel's `scripts/` directory:

```bash
# Set kernel source path
KERNEL_SRC=/path/to/linux-kernel

# Copy generator scripts
cp generate_ninja.py $KERNEL_SRC/scripts/
cp generate_cmdfiles.py $KERNEL_SRC/scripts/

# Make scripts executable
chmod +x $KERNEL_SRC/scripts/generate_ninja.py
chmod +x $KERNEL_SRC/scripts/generate_cmdfiles.py
```

### Step 2: Modify Top-Level Makefile

Edit the top-level `Makefile` in your kernel source tree and add the following sections:

#### 2.1 Add `ninja` to no-dot-config-targets

Find the line starting with `no-dot-config-targets :=` and add `ninja` to the list:

```makefile
no-dot-config-targets := $(clean-targets) \
             cscope gtags TAGS tags help% %docs check% coccicheck \
             $(version_h) headers headers_% archheaders archscripts \
             %asm-generic kernelversion %src-pkg dt_binding_check \
             outputmakefile rustavailable rustfmt rustfmtcheck \
             ninja    # <-- ADD THIS
```

#### 2.2 Add Help Text for Ninja Target

Find the `help:` target and add ninja documentation after the headers_install section:

```makefile
@echo  '  headers_install - Install sanitised kernel UAPI headers to INSTALL_HDR_PATH'; \
 echo  '                    (default: $(INSTALL_HDR_PATH))'; \
 echo  ''
@echo  '  ninja           - Generate build.ninja for the ninja build system'  # <-- ADD THIS
@echo  ''                                                  # <-- ADD THIS
```

#### 2.3 Add build.ninja to Clean Target

Find the `clean:` target and add `build.ninja` to the removal list:

```makefile
-o -name '*.symtypes' -o -name 'modules.order' \
-o -name '*.c.[012]*.*' \
-o -name '*.ll' \
-o -name 'build.ninja' \    # <-- ADD THIS
-o -name '*.gcno' \
\) -type f -print \
```

#### 2.4 Add Ninja Generation Rules at End of Makefile

Add the following block at the end of the top-level `Makefile`:

```makefile
# Ninja build file generation
# ---------------------------------------------------------------------------

quiet_cmd_gen_ninja = GEN     build.ninja
      cmd_gen_ninja = $(PYTHON3) $< -d $(objtree) -a $(AR) --ld $(LD) --cc $(CC) -o build.ninja $(if $(CONFIG_MODULES),modules.order)

ninja: $(srctree)/scripts/generate_ninja.py FORCE
    $(Q)$(MAKE) cmdfiles
    $(call cmd,gen_ninja)

targets += build.ninja

# cmdfiles - Generate .cmd files for all source files without compiling
# This is used by 'make ninja' to generate build rules without actual compilation
# Only depends on basic config, not prepare (which triggers compilation)
# ---------------------------------------------------------------------------
PHONY += cmdfiles

cmdfiles:
    $(Q)$(MAKE) $(build)=. need-builtin=1 need-modorder=1 cmdfiles-mode=1 cmdfiles
```

### Step 3: Modify scripts/Kbuild.include

Edit `scripts/Kbuild.include` and add the following macros after the `cmd_and_fixdep` definition:

```makefile
# Generate .cmd file only, without executing the command
# Used by 'make cmdfiles' to prepare for ninja generation
# Usage: $(call savecmd_only,cmdname) where cmd_cmdname is the command to save
savecmd_only = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(make-cmd)' > $(dot-target).cmd 2>/dev/null || \
    printf '%s\n' 'savedcmd_$@ := $(make-cmd)' > $(dot-target).cmd

cmd_save_only = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(make-cmd)' > $(dot-target).cmd 2>/dev/null || \
    printf '%s\n' 'savedcmd_$@ := $(make-cmd)' > $(dot-target).cmd
```

### Step 4: Modify scripts/Makefile.build

Edit `scripts/Makefile.build` and make the following changes:

#### 4.1 Skip Archive Rules in cmdfiles-mode

Find the `targets-for-builtin` definition and wrap the archive rules:

```makefile
targets-for-builtin := $(extra-y)

# Skip lib.a and built-in.a in cmdfiles-mode since we only generate .cmd files
ifndef cmdfiles-mode
ifneq ($(strip $(lib-y) $(lib-m) $(lib-)),)
targets-for-builtin += $(obj)/lib.a
endif
ifdef need-builtin
targets-for-builtin += $(obj)/built-in.a
endif
endif
```

#### 4.2 Guard Compile Rules with cmdfiles-mode

Find the compile rules (`.o: .c`, `.o: .S`, `.o: .rs`) and wrap them:

```makefile
# Built-in and composite module parts
ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.c $(recordmcount_source) FORCE
    $(call if_changed_rule,cc_o_c)
    $(call cmd,force_checksrc)
endif

# ... (other compile rules)

ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.S FORCE
    $(call if_changed_rule,as_o_S)
endif

ifndef cmdfiles-mode
$(obj)/%.o: $(obj)/%.rs FORCE
    +$(call if_changed_rule,rustc_o_rs)
endif

# ... (archive rules)
ifndef cmdfiles-mode
$(obj)/built-in.a: $(real-obj-y) FORCE
    $(call if_changed,ar_builtin)
endif

ifndef cmdfiles-mode
$(obj)/lib.a: $(lib-y) FORCE
    $(call if_changed,ar)
endif
```

#### 4.3 Guard Build Target

Find the main build target and guard it:

```makefile
ifndef cmdfiles-mode
$(obj)/: $(if $(KBUILD_BUILTIN), $(targets-for-builtin)) \
     $(if $(KBUILD_MODULES), $(targets-for-modules)) \
     $(subdir-ym) $(always-y)
    @:
endif
```

#### 4.4 Add cmdfiles Rules at End of File

Add the following at the end of `scripts/Makefile.build`:

```makefile
# cmdfiles target: generate .cmd files for all source files without compiling
# This allows ninja generation without first running a full make build
# ---------------------------------------------------------------------------

PHONY += cmdfiles

# Commands for .cmd file generation (the actual compile commands to save)
cmd_cmdfiles_c = $(CC) $(c_flags) -c -o $@ $<
cmd_cmdfiles_S = $(CC) $(a_flags) -D__ASSEMBLY__ -c -o $@ $<

# Generate .cmd file for C sources without actual compilation
quiet_cmd_savecmd_c =
      cmd_savecmd_c = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(call make-cmd,cmdfiles_c)' > $(dot-target).cmd 2>/dev/null || \
    printf '%s\n' 'savedcmd_$@ := $(call make-cmd,cmdfiles_c)' > $(dot-target).cmd

# Generate .cmd file for S sources without actual compilation  
quiet_cmd_savecmd_S =
      cmd_savecmd_S = $(objtree)/scripts/basic/fixdep /dev/null $@ '$(call make-cmd,cmdfiles_S)' > $(dot-target).cmd 2>/dev/null || \
    printf '%s\n' 'savedcmd_$@ := $(call make-cmd,cmdfiles_S)' > $(dot-target).cmd

# Pattern rules to generate .cmd files without actual compilation
# Only enabled when cmdfiles-mode=1 to avoid interfering with normal builds
ifdef cmdfiles-mode
$(obj)/%.o: $(src)/%.c FORCE
    $(Q)mkdir -p $(obj) 2>/dev/null
    $(call cmd,savecmd_c)

$(obj)/%.o: $(src)/%.S FORCE
    $(Q)mkdir -p $(obj) 2>/dev/null
    $(call cmd,savecmd_S)
endif

# Get list of source files that exist
cmdfiles-all-objs := $(sort $(real-obj-y) $(lib-y) $(targets))
cmdfiles-c-srcs := $(wildcard $(patsubst $(obj)/%.o,$(src)/%.c,$(cmdfiles-all-objs)))
cmdfiles-S-srcs := $(wildcard $(patsubst $(obj)/%.o,$(src)/%.S,$(cmdfiles-all-objs)))
cmdfiles-objs := $(patsubst $(src)/%.c,$(obj)/%.o,$(cmdfiles-c-srcs)) \
                 $(patsubst $(src)/%.S,$(obj)/%.o,$(cmdfiles-S-srcs))

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

.PHONY: $(PHONY)
```

#### 4.5 Handle Architecture-Specific Dependencies (arm64 Example)

For arm64, also modify `arch/arm64/kernel/Makefile` to guard vdso dependencies:

```makefile
# Force dependency (vdso*-wrap.S includes vdso.so through incbin)
# In cmdfiles mode, skip this dependency since we only generate .cmd files
ifndef cmdfiles-mode
$(obj)/vdso-wrap.o: $(obj)/vdso/vdso.so
$(obj)/vdso32-wrap.o: $(obj)/vdso32/vdso.so
endif
```

### Step 5: Update .gitignore

Add `build.ninja` to your `.gitignore`:

```bash
echo "build.ninja" >> $KERNEL_SRC/.gitignore
```

### Step 6: Configure the Kernel

Ensure your kernel is configured. The `.config` file must exist:

```bash
cd $KERNEL_SRC
make defconfig
# or
make menuconfig
```

### Step 7: Generate .cmd Files

Generate `.cmd` files without actually compiling:

```bash
make cmdfiles -j$(nproc)
```

This step:
- Runs through all directories defined in the kernel build system
- Generates `.cmd` files containing compile/assemble/archive commands
- Completes quickly since no actual compilation occurs

### Step 8: Generate build.ninja

Generate the Ninja build file:

```bash
make ninja
```

Or use the Python script directly with more control:

```bash
python3 scripts/generate_ninja.py \
    -d . \
    -o build.ninja \
    --ar $(which ar) \
    --ld $(which ld) \
    --cc $(which gcc)
```

### Step 9: Build with Ninja

Once `build.ninja` is generated, use Ninja for all subsequent builds:

```bash
ninja                    # Build default targets (vmlinux + modules)
ninja vmlinux           # Build kernel only
ninja modules           # Build modules only
ninja drivers/net/ethernet/intel/e1000e/e1000e.ko   # Build specific module
```

## Make Targets

After integration, these make targets are available:

| Target | Description |
|--------|-------------|
| `make ninja` | Generate `build.ninja` file |
| `make cmdfiles` | Generate `.cmd` files without compiling |
| `ninja` | Build using Ninja (after generation) |

## Architecture Support

The integration supports multiple architectures:

- **arm64** (primary target)
- **x86/x86_64**
- **riscv**
- **arm**
- **mips**
- **powerpc**

Architecture is auto-detected from `.config`.

## File Summary

Files added to kernel tree:

| File | Purpose |
|------|---------|
| `scripts/generate_ninja.py` | Main ninja build file generator |
| `scripts/generate_cmdfiles.py` | Generates .cmd files without compiling |

Files modified:

| File | Changes |
|------|---------|
| `Makefile` | Added `ninja` and `cmdfiles` targets |
| `scripts/Kbuild.include` | Added `savecmd_only` and `cmd_save_only` macros |
| `scripts/Makefile.build` | Added cmdfiles-mode support and guarded compile rules |
| `arch/arm64/kernel/Makefile` | Guarded vdso dependencies (arm64 only) |
| `.gitignore` | Added `build.ninja` |

## Troubleshooting

### Missing .cmd Files

If `make ninja` fails with "No .cmd files found":

```bash
# Ensure cmdfiles ran successfully
make cmdfiles V=1
# Check for errors in specific directories
```

### Regenerating build.ninja

After source file changes (new files, renamed files):

```bash
make cmdfiles && make ninja
```

No need to regenerate for normal code edits - Ninja handles incremental builds.

### Clean Build

To start fresh:

```bash
make clean          # Removes build.ninja and objects
```

Then regenerate:

```bash
make cmdfiles
make ninja
ninja
```

## Uninstallation

To remove the integration:

```bash
# Remove added scripts
rm -f scripts/generate_ninja.py
rm -f scripts/generate_cmdfiles.py

# Revert Makefile changes (manual or use git)
git checkout Makefile

# Revert Kbuild.include changes (manual or use git)
git checkout scripts/Kbuild.include

# Revert Makefile.build changes (manual or use git)
git checkout scripts/Makefile.build

# Revert arch-specific changes (for arm64)
git checkout arch/arm64/kernel/Makefile

# Remove build.ninja from .gitignore
sed -i '/^build.ninja$/d' .gitignore
```

## Limitations

- Initial `make cmdfiles` still uses Make (one-time cost)
- Some generated sources (ASN.1, Device Tree) need special handling
- Kconfig changes require regeneration of build.ninja
- Not all kernel build targets are supported (focus is on vmlinux/modules)

## References

- [Ninja Build System](https://ninja-build.org/manual.html)
- [Linux Kernel Build System](https://www.kernel.org/doc/html/latest/kbuild/index.html)
- [Kernel .cmd file format](https://www.kernel.org/doc/html/latest/kbuild/makefiles.html)
