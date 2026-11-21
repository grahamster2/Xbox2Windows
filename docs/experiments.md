# Experiment notes and reproduction commands

Run commands from the repository root. Start with the [project overview](../README.md) and [contribution guide](../CONTRIBUTING.md). Package-specific observations below used local inputs. Community regression tests use fixtures built from this repository.

## Local tools

For optional static dependency inspection, build Microsoft's pinned version of xbdepends from PowerShell with x64 Visual Studio Build Tools, CMake and the Windows SDK.

```powershell
.\tools\setup-xbdepends.ps1
```

Load the toolchain in each new development shell, then configure, build and test.

```powershell
.\tools\dev.ps1
cmake --preset dev
cmake --build --preset dev
ctest --preset dev
```

The tests exercise desktop D3D12 root signature serialization and deserialization. Use the resulting test binary to try the static inspection tools.

```powershell
xbdepends -v .\out\build\dev\d3d12_sdk_check.exe
dumpbin /imports .\out\build\dev\d3d12_sdk_check.exe
```

Build outputs and downloaded tool sources live under `out/`. [Microsoft xbdepends documentation](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/xbdepends/)

## Source-built EXE entry to a DLL import

MSVC assembles a small CRT-free [EXE entry](../tests/cpu/exe_entry.asm) and links it with the existing `call_mix` fixture. The [translator](../tools/cpu_exe_aot.py) starts at the PE's `AddressOfEntryPoint`, reads its unwind-bounded instruction range, follows the relative call to `call_mix`, and resolves its IAT slot against `cpu_import_provider.dll!mix_args`. It emits both functions as C++. The translated EXE uses ordinary Windows C++ startup to enter the generated `main`. Both versions use the same native provider and the same I/O DLL.

The [Windows test](../tests/cpu/exe_entry_check.py) sends one byte to each child. The provider records its two incoming integers and writes the computed word through its third argument. The I/O DLL sends the recorded integers to stderr and the eight output bytes to stdout. The test reads both pipes and the process exit code.

| Input byte | Integers received by the DLL | Output bytes in write order | Exit code |
| --- | --- | --- | --- |
| `00` | `0x100000303`, `0xfffffffd` | `11 03 00 00 00 00 00 00` | `0x60d` |
| `01` | `0x30`, `0x06` | `35 00 00 00 00 00 00 00` | `0x57` |

A second build flips the selector's conditional jump. The same translator compiles that EXE, and both inverted programs must exchange the two rows in all three observations. Empty input returns 2 with both pipes empty in all four programs. The ordinary process launch and the first exit code follow the contributor's [mapped-process experiment](https://github.com/R0Y00/Xbox2Windows/issues/165). That experiment covers TLS startup separately.

```powershell
cmake --build --preset dev --target cpu_exe_original_lifted cpu_exe_inverted_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_exe_entry$' -V
```

### Running the same EXE under pe_map

`cpu_native_exe_chain` gives the original EXE and its AOT build the same selector byte as above. A third child maps that exact original EXE, binds its ordinary imports to the two source-built Windows DLLs and calls `pe_map_run`. The child passes the guest's returned DWORD back as its process exit code. All three paths produce the same 16 argument bytes, eight output bytes and exit codes in the table.

The first host dependency appears during IAT binding, before any guest instruction runs. On the tested MSVC build, an empty binding table stops at `cpu_exe_io.dll!emit_trace`; supplying just the I/O DLL stops at `cpu_import_provider.dll!mix_args`. The first import invoked at runtime is `read_selector`. With both DLLs bound, the entry and its internal caller execute unchanged. This measures the source-built Windows fixture and its providers.

```powershell
cmake --build --preset dev --target pe_map cpu_exe_original_lifted
ctest --preset dev --no-tests=error -R '^cpu_native_exe_chain$' -V
```

## Pipeline readiness from shader manifests

`tools/pso_readiness.py` joins the native SHA-256 and stage in the shader extraction manifest with explicit pipeline references. It counts archive records, records with a native hash and unique stage/hash pairs separately. Multiple archive records can refer to the same native code. A hash from one stage does not establish coverage in another stage.

Export the archive manifest with `extract_shaders.py --format native` or `--format both`. Supply a separate JSON file containing pipeline IDs, their effective root signature status and shader references. Here's a synthetic example. The zero hash stands for a missing archive entry.

```json
{
  "pipelines": [
    {
      "id": "example-pixel-pipeline",
      "effective_root_signature": "unknown",
      "shaders": [
        {
          "stage": "pixel",
          "sha256": "0000000000000000000000000000000000000000000000000000000000000000"
        }
      ]
    }
  ]
}
```

Set `effective_root_signature` to `present`, `missing` or `unknown` after accounting for both the embedded signature and any signature provided by the caller. The report accepts normalized references from an inspection tool or a trace and assigns no references from archive proximity.

```powershell
python -B tools/pso_readiness.py out/shaders/manifest.json out/pipelines.json
cmake --build --preset dev --target pso_root_check
ctest --preset dev --no-tests=error -R '^pso_(readiness_check|root_signature_warp)$' -V
```

Add `--llvm <AMDGPU LLVM library>` to disassemble referenced native pixel code and run the existing pixel scanner and lowering probe. Each unique stage/hash pair is assessed once. A known missing root signature takes precedence over shader diagnostics. Missing references, unassessed stages and unknown signature status require more evidence. Passing the pixel probe leaves the pipeline awaiting PSO validation, since the scanner uses synthetic bindings.

The WARP control compiles the source-built compute shader with its root signature stripped. D3D12 rejects creation without a root signature. The same shader creates a PSO when given the matching signature from the embedded build. The control measures PSO creation on the source-built fixture.

## Offline CPU leaf recompilation

With the Python-enabled Windows dev preset configured, run:

```powershell
cmake --build --preset dev --target cpu_leaf_original_lifted cpu_leaf_inverted_lifted
ctest --preset dev --no-tests=error -R '^cpu_leaf_aot_branches$' -V
```

MSVC's x64 assembler and linker build [the original function](../tests/cpu/branch.asm) into a DLL. The [translator](../tools/cpu_leaf_aot.py) reads its exported entry RVA and the matching runtime-function interval from the PE. It translates those bytes into C++, which MSVC compiles to a separate x64 object and links into a second DLL. The intermediate source is in `out/build/dev/cpu_leaf_original_lifted.cpp`.

The supported signature takes an unsigned 32-bit integer in ECX and a writable word pointer in RDX, and returns a 32-bit integer in EAX. The decoder covers the fixture's register move, immediate compare paired with unsigned short JB/JAE, short forward jump, immediate add/subtract, output store and return. It rejects unknown encodings and branches outside the decoded instruction boundaries. Leaf functions can omit unwind metadata. This experiment uses an explicit interval provided by the assembler.

The Windows test calls both DLLs with the same 263 inputs. Five take the below-five branch and 258 take the other branch, including equality, the unsigned sign boundary and wraparound. It compares the returned word, the stored word and guards on either side, and checks the original against independent unsigned arithmetic. A control assembles the same source with the conditional jump inverted and runs that PE through the same translator. Its results must differ from the original on every input. [Direct-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/22)

## Chained function ranges and early returns

The range reader groups runtime-function entries by their complete unwind-chain parent records. For chained trailer lookup it accepts both version 1 and version 2 `UNWIND_INFO` headers when `UNW_FLAG_CHAININFO` is the only flag set, without decoding general version-2 unwind codes. The early-return translator uses iced-x86 to decode each owned interval separately. It follows instruction-aligned branches and fallthrough, models stack slots and preserved registers, and checks their restoration at each return. A neighboring function can occupy the gap. The single-interval translators report split functions explicitly. [Microsoft chained unwind information](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64#chained-unwind-info-structures)

```powershell
cmake --build --preset dev --target cpu_chained_lifted cpu_chained_jne_lifted cpu_chained_zero_lifted cpu_chained_imm_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_chained_ranges$' -V
```

The source-owned fixture has a primary interval, two levels of chaining and a neighboring export, built in `JE`, `JNE`, self-`XOR` zeroing, and general 32-bit immediate (`edx`/`r8d`) variants. Both null inputs return 7 (or 0 in the `XOR`-zeroing variant), and two non-null inputs return 21. A Windows probe compares native returns with the generated result, physical RBX/RSI, and guards. A patched copy verifies version-2 chained header resolution. Negative controls cover broken and cyclic parent links, unsupported chained header versions and combined flags, an unsupported conditional branch after `TEST`, a non-self `XOR`, clobbering an unsaved callee-saved register with a 32-bit immediate, jumps into a neighbor or instruction interior, and an indirect call.

For the local FidelityFX loader, run `cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_context.cpp --partial` using the Python dev environment. Compile the generated C++ as a Windows x64 DLL. Its `try_lifted(first, second, output)` returns 1 and writes the guest result for a completed path. Status 0 leaves the output untouched at an unsupported indirect call. Pointer arguments must reference readable fixture storage on paths that dereference them. The local inspection recovered four owned intervals. Running the generated code produced 6 for a null first argument and for a pointer to a null context. A valid non-null control stopped at the first indirect call with its output sentinel intact. The original package DLL was inspected as a file. Native differential results above are from the repository fixture. [Callback follow-up](https://github.com/R0Y00/Xbox2Windows/issues/29)

### Object-table callbacks on Windows

The source-built caller follows a handle to its context and loads a table pointer from that context. Slot 1 takes the context address and a 64-bit input and returns a 64-bit value. The test selects two tables with the providers in opposite orders. It compares the observed provider, both arguments, return and output guards with native Windows execution, including inputs with bit 63 set. Both null paths return 6 without calling either provider.

```powershell
cmake --build --preset dev --target cpu_callback_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_object_callback$' -V
```

For this fixture, `cpu_chained_aot.py` takes `--callback-offset 8` and emits `try_lifted_callback(first, second, output, binding)`. The host supplies readable fixture storage and a binding containing the expected table address and typed function pointer. Dispatch requires both the table address and the selected slot's target to match. A missing or mismatched binding returns status 0 and leaves the output untouched. The existing three-argument partial profile still stops at an unbound indirect call.

### FidelityFX destroy provider and cleanup

In the inspected Xbox loader, `ffxCreateContext` masks the effect ID and calls the internal resolver at RVA `0x1000`. For effect `0x10000` the resolver gets or loads `amd_fidelityfx_upscaler_xbox.dll`. It resolves the named export `ffxDestroyContext` and stores the address at loader RVA `0x4098`, slot 1 of the table at `0x4090`. After a successful provider create, the loader puts that table pointer and the provider context in the two words of a wrapper. The packaged upscaler export directory independently identifies the destroy entry at provider RVA `0x13a0`. The frame-generation route uses another table and its module is absent from this package.

The loader passes the **address of the wrapper's provider-context field** in RCX and the optional allocation-callbacks pointer in RDX. The upscaler dereferences the first argument and saves the second in a local slot. Its internal destroy method receives the effect object, the context-field address and the address of that saved allocator pointer. On the provider-object release path the upscaler also uses the allocator for deallocation or calls CRT free. These register uses agree with AMD's two-argument destroy declaration and unsigned 32-bit status. [AMD FidelityFX API header](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

After provider destroy, the **loader** frees its own wrapper, including when the provider returned an error. Custom cleanup receives the allocator's user data and the wrapper address. The default branch calls the imported CRT free. Both branches return the saved provider status. The explicit `--ffx-destroy` profile binds a stable provider table, allocator and cleanup routines for the duration of the call. The host must supply live storage compatible with the chosen deallocator. An unresolved binding stops before invoking the provider.

```powershell
cmake --build --preset dev --target cpu_destroy_lifted cpu_destroy_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_destroy_contract$' -V
```

CI compares a source-built native caller with its static translation. On Windows the same harness also compared that native reference with the translation of all four owned ranges of the local loader export. Both null exits returned 6. With a source-built provider resolved by export name, both cleanup routes preserved 0, 6, 0x80000001 and 0xffffffff. The test checks the addresses passed to destroy and custom cleanup, and that the provider clears the inner field before the wrapper is released. The packaged upscaler's own effect teardown was inspected statically.

To repeat the local comparison in a VS x64 developer shell, translate the loader with `tools/cpu_chained_aot.py <loader.dll> ffxDestroyContext out/local_destroy.cpp --ffx-destroy`, compile it with `cl /nologo /LD /O2 /Fo:out/local_destroy.obj out/local_destroy.cpp /link /OUT:out/local_destroy.dll /IMPLIB:out/local_destroy.lib`, and run `tests/cpu/destroy_check.py` with absolute paths to `cpu_destroy.dll`, `local_destroy.dll` and `cpu_destroy_provider.dll`, in that order.

### First device calls in the packaged upscaler

For the upscaling effect (`0x10000`), the local upscaler's `ffxCreateContext` export at RVA `0x11d0` takes an output handle pointer in RCX, a descriptor chain in RDX, and an optional allocator in R8. On the inspected path, the backend extension has type `0x01000002` at offset 0, a next pointer at +8 and the device pointer at +16. The provider selection call at `0x129c` passes that pointer to a predicate that returns true without reading it. At `0x1349` the export calls the selected provider's create method at `0xc9b0`.

The provider reads the device from the extension at `0xcdca`, sets up a fresh 0x18f160-byte scratch buffer for one context and invokes its backend callback at `0x2720`. At the call site RCX points to the backend interface, EDX is `0x14`, R8 points to four 32-bit words `{0, 0, 0, 1}`, and R9 points to storage for the output context index. The callback reads the device at interface +0xd0 and scratch at +0xc0. On its first use, the device receives `IUnknown::AddRef(this)` at `0x2803`. It takes only the device in RCX. The callback stores the device at scratch +8 and continues without inspecting the returned reference count.

The first resource creation at `0x2893` uses device vtable slot 14 (+0x70). RCX holds the device, RDX points to a 16-byte `D3D12_DESCRIPTOR_HEAP_DESC`, R8 points to the IID of `ID3D12DescriptorHeap` (`8efb471d-616c-4f49-90f7-127bb763fa51`), and R9 points to the output slot. All five calls request node mask 0. [D3D12 CreateDescriptorHeap](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createdescriptorheap) · [IUnknown AddRef](https://learn.microsoft.com/en-us/windows/win32/api/unknwn/nf-unknwn-iunknown-addref)

| Call RVA | Heap type | Descriptors | Shader visible | Output in scratch |
|---|---|---:|---|---|
| `0x2893` | CBV/SRV/UAV | 512 | No | +0x28 |
| `0x28ac` | CBV/SRV/UAV | 512 | No | +0x30 |
| `0x28cd` | CBV/SRV/UAV | 512 | Yes | +0x38 |
| `0x28fd` | CBV/SRV/UAV | 167,936 | Yes | +0x48 |
| `0x2935` | RTV | 8 | No | +0x20 |

On Windows the probe starts at RVA `0x27f8` with a live device, zero-initialized scratch, a context capacity of one and R15 zero. It executes the offline translation of the package's argument construction and binds the two COM methods to desktop D3D12. The RTX 4060 Laptop (driver 32.0.16.1656) and Microsoft WARP (10.0.26100.9818) each returned `S_OK` five times. The probe verified five distinct live heap handles, their output slots and their descriptions. Both debug-layer runs reported zero errors and zero warnings. The host releases the five heaps and its device reference on exit.

After the fifth heap the test continues into the backend slot scan at RVA `0x2938`. The host stops on a failed HRESULT so that a desktop failure identifies the exact heap. The package slice itself has no intervening HRESULT branches.

To run the local translation in a VS x64 developer shell, set `$upscaler` to the full path of the package's upscaler DLL and use a separate build directory. The translator checks the image hash before generating C++.

```powershell
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE:FILEPATH=$upscaler"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_device_init_' --no-tests=error -V
```

The log identifies this run with `source=translated-package`. The default build uses a source-owned desktop control for the five D3D12 calls (`source=desktop-control`). Public CI runs that control on WARP and compares native and translated x64 heap-size arithmetic for six inputs, including 32-bit wraparound.

### Backend context slot selection

The provider takes the address of its 32-bit index field at +0xec (`0xcf94`) and passes it in R9. The backend callback saves that pointer in R14. On first use it finishes setting up the shared descriptor range at `0x2938`. Subsequent calls skip heap creation and rejoin at `0x2942`. The callback increments the 32-bit scratch counter before checking capacity, then scans from index zero. The table pointer is at scratch +0x1078, records are 96 bytes each, and bit 0 of the byte at record +0x3c means occupied.

At the first free slot, the store at `0x2980` writes the index directly to the provider. The callback writes effect `0x14`, sets flag bit 0, clears bit 1 and preserves the other six bits. It initializes two pairs of descriptor cursors to `512*i+1` and `512*i+511`. On the package's one-slot input, the four sizes `{0,0,0,1}` reserve the half-open range `[0x19000,0x19001)`. When all slots are occupied, the scan branches to the same zero-return exit at `0x2a9f` without writing a record or the output index.

| Windows input | Backend return | Provider index after call | Scratch counter | Slot change |
|---|---:|---|---|---|
| Fresh package table after five heaps | 0 | 0 | 0 → 1 | Slot 0 becomes occupied |
| Same table, second request | 0 | `0xfeedface` preserved | 1 → 2 | None |
| Controlled two-slot table, slot 0 occupied | 0 | 1 | 1 → 2 | Slot 1 becomes occupied. Its range starts after slot 0 at `0x32008` |
| Both controlled slots occupied | 0 | `0xfeedface` preserved | 2 → 3 | None |

The package translation and an independently compiled C++ control ran on the RTX 4060 Laptop and WARP using the commands above. The test compares the entire scratch buffer and provider output region, including bytes around the index. The exhaustion cases supply a new output field initialized to the sentinel. The dual-slot setup exercises a nonzero index with a controlled capacity and four nonzero sizes. Public CI runs the desktop control plus a source-built x64 scan and four-lane sum in `cpu_aot_heap_layout`. The same fixture compares 32-bit registers with sign-extended 8-bit immediates such as `cmp ecx,-1`, which the lowerer evaluates at the operand width.

The provider tests EAX at `0xcfc2`. A zero backend return continues initialization. It later loads the same index field into R8D at `0xd1f4` for the resource callback at `0x2e30`. On the default path the first descriptor identifies `FSR4UPSCALER_Recurrent` (ID 13), with its allocation mode and placed-resource flag both zero and the optional allocation callback unset. The intervening descriptor-builder call at `0xc4ff` resolves to a CPU-only helper at `0x7900`, and the import at `0x2ea1` is an XMem working-set query. The next call **on the device** is at `0x33cc`, Xbox vtable slot 76 (+0x260). Its arguments include heap properties, zero heap flags, a resource description, initial state, a null clear value, the `ID3D12Resource` IID and an output pointer. Those arguments match the shape of desktop committed-resource creation. The Windows replay below binds that call to the desktop device. [Desktop CreateCommittedResource signature](https://learn.microsoft.com/en-us/windows/win32/api/d3d12/nf-d3d12-id3d12device-createcommittedresource)

### Creating the first recurrent texture

For the default provider, the builder at `0xbf60` receives the output-size pair in R8. The create descriptor supplies render width/height at +0x14/+0x18 and output width/height at +0x1c/+0x20. The first resource uses the latter pair and has ID 13 and name `FSR4UPSCALER_Recurrent`. The local replay translates the first descriptor stores, the provider's index-output address calculation and the default committed-resource path. It supplies zero for the optional Xbox working-set statistics and uses the default allocator route with the optional resource allocation callback unset.

| Field | Package path | Desktop D3D12 argument |
|---|---|---|
| Dimensions | Kind 2, output width and height, one layer, one mip | Texture2D, one sample, quality 0 |
| Format | Format code 10 selects value 28 in the format switch at `0x2160` | `DXGI_FORMAT_R8G8B8A8_UNORM` |
| Flags | Usage bits 0 and 1 produce flags 0x5 | `ALLOW_RENDER_TARGET` and `ALLOW_UNORDERED_ACCESS` |
| Initial state | Provider state 2 selects value 8 at `0x325a` | `D3D12_RESOURCE_STATE_UNORDERED_ACCESS` |
| Allocation | Default heap, zero heap flags, null optimized clear value | Committed resource, default alignment, single-node masks 1/1 |
| Layout | Xbox texture layout 0x101, extension word 3 and trailing zero | Driver-chosen `D3D12_TEXTURE_LAYOUT_UNKNOWN`. The adapter checks the Xbox extension fields before omitting them from the desktop descriptor |

With a render size of 1280×720, the RTX 4060 Laptop (driver 32.0.16.1656) and WARP (10.0.26100.9818) each ran two fresh contexts. For outputs of 1920×1080 and 1600×900, all four calls returned `S_OK`. Each resource's `GetDesc` reported the requested output dimensions, format 28, flags 0x5, one layer/mip/sample, quality zero, desktop layout 0 and 65,536-byte alignment. Initial state is a creation argument. The probe transitions the texture from UAV to shader read, then copy source, then back to UAV and waits for completion. Both debug layers reported zero errors and zero warnings.

On context slot 0 the provider passes +0x17c as the output for ID 13. The resource callback takes cursor 1, advances it to 2 and writes 1 to the output **before** the device call. On success it records provider state 2 in both state fields and stores the returned COM pointer in backend record 1 at `0x3408`. The test checks that pointer against the live texture and verifies all adjacent resource indices retain their sentinels.

Use the package build directory configured above in a VS x64 developer shell. Public CI runs the source-owned WARP fixture with the same desktop mapping and readback checks. It also compares native and translated descriptor-copy instructions using the MASM fixture.

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_recurrent_' --no-tests=error -V
```

### Recurrent texture views and pixel readback

After the resource pointer is stored, the callback reads its description and builds views for the single-layer, single-mip Texture2D path. The SRV and UAV format switches both preserve `DXGI_FORMAT_R8G8B8A8_UNORM` (28). The SRV exposes mip 0 only, plane 0, minimum LOD 0 and the default RGBA component mapping (`0x1688`). Both UAV descriptors select mip 0 and plane 0 and use a null counter.

| Device call RVA | Descriptor | Heap pointer in scratch | Slot in fresh context 0 |
|---|---|---|---:|
| `0x37aa` | SRV | +0x28, CPU-only | 1 (resource index) |
| `0x3844` | UAV | +0x38, shader-visible | 1 (UAV base + mip) |
| `0x388a` | UAV mirror | +0x30, CPU-only | 1 (UAV base + mip) |

The resource record stores the UAV base at +0x34 and the count at +0x38, both 1. After creating both UAV descriptors, the callback advances the context's UAV cursor at +0x0c from 1 to 2. Each destination is the heap's CPU start plus the slot times the device-reported descriptor increment. That increment was 32 bytes on the RTX 4060 Laptop and 64 bytes on WARP.

The same `ffx_recurrent_hardware` and `ffx_recurrent_warp` commands above run the pixel check. A source-built compute shader writes a coordinate-dependent RGBA pattern through the original shader-visible UAV. A second pass writes a different pattern through a copy of the CPU UAV mirror. For shader access the probe copies the CPU-only SRV and UAV mirror into a separate, shader-visible test heap. A read shader samples the SRV at texel centers and writes packed RGBA bytes to a buffer. A separate `CopyTextureRegion` reads the original resource pointer directly. After a fence, both paths must match the CPU's expected RGBA value for every pixel. [D3D12 descriptor copying](https://learn.microsoft.com/en-us/windows/win32/direct3d12/copying-descriptors)

Both adapters passed both patterns at 1920×1080 and 1600×900. Each adapter checked 7,027,200 pixels through the SRV and the same number through direct copies, with zero mismatches, debug errors or warnings. The local build executes the package-derived view creation through the cursor update at RVA `0x389b`. Public Windows CI runs the source-owned WARP control. The test shaders exercise descriptor access. The provider loop is covered below.

### Provider resource loop and returned handles

At 1280×720 render and 1920×1080 output, the default provider visits twelve candidate descriptors in order. The builder at RVA `0xbf60` emits 104 bytes per candidate. The loop at `0xd1d0` skips ID 23 when bit 9 of the create flags is clear. The index destination is `provider + 0x148 + 4 * ID`. Three callbacks also allocate an upload resource from the same cursor.

| ID | Resource (FSR4UPSCALER_ prefix) | Shape | Default index | Index with flag 0x200 |
| ---: | --- | --- | ---: | ---: |
| 13 | Recurrent | 1920×1080 RGBA8 | 1 | 1 |
| 14 | History | 1920×1080 RGBA16F | 2 | 2 |
| 15 | HistoryReprojected | 1920×1080 RGBA16F | 3 | 3 |
| 16 | RcasIntermediary | 1920×1080 RGB9E5 | 4 | 4 |
| 5 | AutoExposure | 8-byte raw buffer | 5 | 5 |
| 17 | DefaultExposure | 1×1 RG32F | 7 | 7 |
| 6 | SpdAtomicCounter | 1×1 R32_UINT | 9 | 9 |
| 7 | Luma_Mip_5 | 80×45 R32F | 11 | 11 |
| 22 | ScratchBuffer | 21,045,760-byte raw buffer | 12 | 12 |
| 20 | InitializerBuffer | 131,072-byte raw buffer | 13 | 13 |
| 21 | InitializerBuffer_Upload | 131,072-byte upload buffer | 14 | 14 |
| 23 | DebugInformation | 1920×1080 R16_UNORM | 0 (skipped) | 15 |

The uploads for IDs 5, 17 and 6 occupy slots 6, 8 and 10 respectively. ID 21 is a named provider resource with its own index. The package row loop treats the zero height of ID 5 as zero rows to initialize, despite its eight-byte input payload. ID 17 supplies two float ones and ID 6 supplies a zero fill. The desktop binding uses committed resources, creates SRVs and paired UAVs for eligible resources, and submits the initialization copies with a fence. Default-heap buffers use desktop COMMON state and implicit promotion.

On success, the translated loop copies all 25 indices to the mirror at +0x1ac and writes the provider pointer through the output argument at RVA `0xd263`. The loader returns a separate 16-byte wrapper containing the callback table and that same provider pointer. With flags 0 and 0x200 both adapters returned 0, with next resource indices 15 and 16 respectively. Injecting a callback return of 7 at ID 20 returned 3 through the provider and loader, left both output handles null, preserved the previous resource indices, and left the mirror untouched. The test checks every live backend record against its D3D12 object, both index tables, and the loader wrapper fields. All three cases completed with zero debug errors or warnings on the RTX 4060 Laptop and WARP.

The first desktop format difference occurs at ID 16. WARP exposed typed RGB9E5 UAV stores. The RTX driver (32.0.16.1656) lacked that capability, so the binding created an R32_UINT texture and views for the packed bits. The texel test below checks the two storage formats.

The local build translates the descriptor builder, debug flag, loop and writeback, resource-slot reservation, upload-descriptor construction, and selected create-export branches. Registry selection and the backend device callbacks are bound on the host. When the pinned loader DLL is beside the upscaler DLL, the probe also executes the recompiled loader create path against that provider. Public CI runs the source-owned provider control on WARP. Configure the local package build as in the recurrent texture test above and run:

```powershell
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

### RCAS intermediate texel access

The provider-loop CTests above also exercise resource ID 16 after successful context creation. The test uses the provider's UAV and SRV for the intermediate at resource index 4. A source-built compute shader writes a strip of 1,024 HDR colors into the 1920×1080 texture. Another shader loads the texels through the SRV. A separate texture copy returns the underlying 32-bit words.

On the RTX 4060 Laptop the writer packs RGB into an R32_UINT UAV and the reader unpacks it to floats. The shared HLSL helpers clamp finite inputs to [0, 65408], choose a shared exponent and round the three 9-bit mantissas to nearest even. On WARP the typed RGB9E5 views perform the conversion. A second WARP context forces the packed path to exercise the fallback in public CI. A double-precision CPU reference checks the stored bits and all four decoded components, including the implied alpha of 1. The inputs cover all 32 exponents, channel permutations, half steps, exponent carry, underflow, negative values and saturation. The native reference also accepts the documented nearest rounding alternative at ties. [Direct3D RGB9E5 conversion](https://microsoft.github.io/DirectX-Specs/d3d/archive/D3D11_3_FunctionalSpec.htm#19.3.2)

On both local adapters the storage words and decoded channels matched nearest-even on every input. Both debug layers reported zero warnings and errors. The package supplies the local resource-creation path. These shaders test integer-coordinate texel loads. The next experiment traces the first package writer and reader of this intermediate.

The integrated AMD Radeon(TM) Graphics of a Ryzen 5 7600 (RDNA 2, device `164e`, driver 32.0.21045.29003) reports typed UAV stores for RGB9E5, so the native path runs on a physical GPU. Its stores round the mantissas toward zero. In 317 of the 1,024 inputs this gives a different word from nearest-even, and every stored word matches a round-toward-zero encoding. The SRV loads decode those words exactly. The forced packed context on the same adapter still matches nearest-even. For FLOAT -> RGBE, section 19.3.2.2 of the D3D11.3 functional spec says conversion "should be done with nearest even. Round to nearest is allowed", and notes that hardware does not perform it. Its general rule for conversion to another float format, in section 3.2.2, is round-to-zero. The native check therefore accepts nearest-even, round-to-nearest or round-toward-zero and reports how many texels needed each alternative. A store that mixes the two alternatives fails. The Xbox Series X GPU is also RDNA 2; whether its native stores round the same way is untested.

```powershell
ctest --preset dev --no-tests=error -R '^ffx_provider_loop_hardware$' -V
```

### FSR4 dispatch through the RCAS input

With sharpening enabled, the default i8 graph first touches the physical ID 16 texture in `FFX_FSR4_PASS_26`. It writes `rw_mlsr_output_color` (logical ID 10, UAV `u2`). The following `FFX_FSR4_RCAS_PASS` reads `r_rcas_input` (ID 16, SRV `t18`) and writes the external `rw_rcas_output` (ID 11, UAV `u11`). Each RCAS lane computes four output pixels. For each pixel it loads the center and four axial neighbors at integer texel coordinates. The native program contains 20 RGB `image_load` instructions with half-precision returns, four stores and zero sampler bindings.

| Provider RVA | Evidence from the pinned upscaler |
|---|---|
| `0xed10`, `0x1220f`, `0x13278` | Dispatch copies the sharpening byte into the staged constants at offset `0x5c`. When enabled, mirror entry 10 receives primary entry 16. When disabled, mirror entries 10 and 11 both receive the external output. |
| `0x1357d`, `0x13752`, `0x1375f` | The graph builder supplies workgroup counts. The scheduling loop indexes the pipeline array with stride `0x9690` and calls `0xe1d0`. The reflection check walks all 27 graph shader records in order. Case 26 selects the container at `0xa0e90`. |
| `0xe244`, `0xe2b9`, `0xe363`, `0xe3cd` | The scheduler resolves SRVs through the primary resource array and UAVs through the mirror, sets the job type to compute (2), then calls the enqueue callback. |
| `0x137b2`, `0x13898`, `0x138ac` | The sharpening flag gates the extra RCAS job. The separate pipeline at provider offset `0x248` selects the RCAS container at `0x701b0`. The provider then calls the execute callback. |

The package build translates the x64 alias updates and the two grid calculations. At 1920×1080 they produce 60×34×1 and 120×68×1 groups. The GPU replay uses an 8×4 logical image, one group per job, on the actual provider intermediate at index 4. Pass 26 has 256 threads per group and RCAS has 64. The host binds source-owned scene and history inputs as RGB buffers, uses unit exposure, and uses the provider's history and recurrent UAVs. Eight active lanes take the producer's direct texture path. The next experiment connects the two immediately preceding graph jobs through scratch.

Both desktop shaders come from offline decoding of the pinned native streams (3,482 producer instructions before specialization and 437 RCAS instructions). LLVM 15's gfx1030 decoder covers the producer's i8 dot instructions and its gfx1013 decoder covers RCAS. The translator keeps the original packed literal DWORDs, including the zero high half of the packed offset that decrements x. [AMD RDNA 2 instruction set](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

The GPU test checks all 32 intermediate texels and 257 surrounding guard texels. It then compares all 256 final pixels, including edges between different colors, with an independent FP32 five-tap reference (per-channel tolerance 2.5% plus 0.00005 for packed-half arithmetic). Alpha must equal red. RTX 4060 Laptop passed using R32_UINT. WARP passed with native RGB9E5 and with forced R32_UINT. Both debug layers reported zero errors and warnings. These are controlled single-group results.

From a VS x64 developer shell at the repository root, set `$upscaler` to the pinned local DLL. The middle command needs the LLVM 15 C library. On Windows, the official LLVM 15.0.7 installer (`winget install --id LLVM.LLVM --version 15.0.7 --exact`) provides `LLVM-C.dll` with the AMDGPU disassembler, so no WSL is required. All extracted programs and generated shaders stay under `out/`.

```powershell
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_profile.py $upscaler out/ffx-dispatch
./out/pe-env/Scripts/python.exe -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm "C:/Program Files/LLVM/bin/LLVM-C.dll"
# Alternatively, in WSL with libLLVM 15 installed:
# wsl -d Ubuntu-22.04 -- python3 -B tools/ffx_dispatch_aot.py out/ffx-dispatch --llvm libLLVM-15.so.1
cmake --preset dev -B out/build/ffx-device "-DFFX_UPSCALER_IMAGE=$upscaler" "-DFFX_DISPATCH_SHADER_DIR=$PWD/out/ffx-dispatch"
cmake --build out/build/ffx-device --target offscreen_check
ctest --test-dir out/build/ffx-device -R '^ffx_provider_loop_' --no-tests=error -V
```

The local log reports `dispatch=package-aot`. Through the native Windows route above (LLVM 15.0.7 `LLVM-C.dll`, Python 3.14.6), the translator decoded 1152, 100, 3482 and 437 instructions for pass 24, pass 25, the producer and RCAS. `ffx_provider_loop_hardware` passed on an RTX 4070 Ti (driver 32.0.16.1088) and `ffx_provider_loop_warp` passed on WARP, both with the debug layer enabled. Each reported `dispatch=package-aot` with 256 final pixels and zero mismatches for the bypass, zero and pattern scratch cases. Public Windows CI runs source-built descriptor and dispatch controls on WARP (`dispatch=desktop-control`). Linux CI assembles an independent integer-coordinate regression for the translator.

### First pass 26 LDS write block

At `0x2410`, the existing 8×4 dispatch skips to `0x26d8`. An optional D3D11 probe models wave zero of group (0,0) with 32 synchronized threads. It decodes the branch predicate and the first block's store addresses from the pinned producer. Distinct synthetic values identify each row, lane and component after the colour transform.

| Input | X scale (Y = 1) | Active lanes | Footprint | Tagged words |
| --- | ---: | ---: | --- | ---: |
| 32×4 | 243/256 | 32 | 32×6 | 576 |
| 32×4 | 244/256 | 32 | 33×6 | 0 |
| 31×4 | 243/256 | 30 | Unspecified | 0 |

On Windows build 26300.9974, NVIDIA GeForce RTX 4060 (610.88) and Microsoft WARP each matched an independent CPU predicate for 120 inputs. An inclusive horizontal limit changed only the 244/256 boundary case at 32×4 and unit vertical scale. All 576 shared-memory words matched their expected order or zero fill for each of the three rows on both adapters. D3D11 debug layer disabled. This probe covers the first gate and tagged stores. The production translator still skips the block. Later LDS reads and pixel output on this path remain unverified.

The Windows build compiles the readback host. The default CTest uses a source-owned HLSL fixture on WARP, exercising the same gate cases, inclusive-limit negative control and tagged stores against the CPU expectations. From the repository root, after configuring the dev preset:

```powershell
cmake --build --preset dev --target ffx_pass26_lds
ctest --preset dev --no-tests=error -R '^ffx_pass26_lds_warp$' -V
```

The physical adapter and pinned package are independent opt-ins. Use the producer generated by the dispatch profile above and a trusted LLVM 15 installation for the package comparison. Omit `--package` to run the source-owned input, or use `--backend hardware` for either input.

```powershell
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend hardware
out/pe-env/Scripts/python.exe -B tests/ffx_pass26_lds/verify.py --host out/build/dev/ffx_pass26_lds.exe --backend warp --package out/ffx-dispatch/producer.bin C:/LLVM15/bin/LLVM-C.dll
```

### Last writers of the pass 26 scratch reads

The default FSR4-i8 graph with tier zero, unit render/output ratio and sharpening enabled schedules passes 24 and 25 immediately before pass 26. At provider RVA `0x136a7` the inclusion mask has bit 25 set. Other scheduling modes can skip the padding job. In the 8×4 case the host rounds both dimensions up to multiples of eight at `0x11f94`. Tensor 13 uses half that aligned size (4×4). The dispatch builder at `0x7e1c` and its padding helper at `0x7730` yield grids of 1×4×1 and 2×1×1 groups respectively.

Both kernels bind the provider's raw scratch UAV at `u11` (resource ID 22, index 12). Pass 24 performs its i8 convolution and stores 16 bytes at shader PC `0x218c`. With plane base `0x7f4bc0`, row stride `0x3c20` and 16 bytes per column, its output starts at column 1, row 1. The launched 64-wide groups also write beyond the four active columns. Pass 25's store at shader PC `0x200` writes four zero DWORDs. Its 44 active lanes cover the top and bottom rows, the left column and five columns of right padding around the 4×4 interior.

Pass 26 has eight active lanes with cell coordinates `(x,y)` in `[0,4) × [0,2)`. Its nine loads read at `base + (y+dy)*stride + 16*(x+dx)`. For a given address, interior columns 1–4 on rows 1–3 of the read window have pass 24 as last writer. All other addresses in that window have pass 25 as last writer.

| Pass 26 shader PC | (dx, dy) | Lanes reading pass 24 | Lanes reading pass 25 |
|---|---|---:|---:|
| `0x14c` | (0, 0) | 3 | 5 |
| `0x154` | (1, 0) | 4 | 4 |
| `0x15c` | (2, 0) | 3 | 5 |
| `0x164` | (0, 1) | 6 | 2 |
| `0x16c` | (1, 1) | 8 | 0 |
| `0x174` | (2, 1) | 6 | 2 |
| `0x17c` | (0, 2) | 6 | 2 |
| `0x184` | (1, 2) | 8 | 0 |
| `0x18c` | (2, 2) | 6 | 2 |

The reproduction commands above also translate the two upstream shaders and use four 64-byte constant ranges from the pinned model initializer. The test seeds the pass 24 input with either zero or a deterministic pattern, fills the output region with a sentinel, and uses the same GPU buffer across all three passes. It copies the 6×4 read window after each writer and records the address and all four DWORDs at each of the 72 pass 26 loads. A third control bypasses both writers and supplies zero scratch directly to pass 26. Model biases can produce nonzero activations even from a zero upstream input.

A separate Python evaluator checks the scratch words and the four history-blend logits per lane using signed-byte dot products. Spatially constant, distinct scene and history colors give a per-pixel blend reference for resource 16. The existing five-tap FP32 reference checks all 256 final RCAS pixels, including the surrounding pattern. Switching the upstream input changed all 32 active pixels at both stages on RTX 4060 Laptop, native RGB9E5 WARP and forced R32_UINT WARP. Both adapters reported zero debug warnings and errors. The tensor feeding pass 24 remains a fixture; the full-wave LDS branch of pass 26 is a separate open path.

### FidelityFX context creation and ownership

The Windows test calls the translated loader create function, then passes its returned handle directly to the existing destroy translation. Create binds a source-owned upscaler provider to the two table slots. On success, the loader allocates a 16-byte wrapper and stores the table pointer and provider context in it. A provider create error propagates its 32-bit return code, clears the caller's handle and skips wrapper allocation. Destroy calls the provider with the address of the inner field before releasing the wrapper, including on a provider destroy error.

Fourteen cases cover default CRT allocation and custom allocation with user data. For each route the test checks success, two destroy errors, three create errors, and failure to allocate the wrapper after the provider succeeds. It observes allocation and release addresses, order, sizes and the live-allocation count. In the allocation-failure case the host adapter calls provider destroy once and returns memory error 5. This rollback is a Windows adapter behavior. The inspected loader proceeds to a wrapper store after its allocator returns. The provider used in this comparison owns real CRT memory and has controlled return codes; creation of a live upscaling effect on a graphics device remains open.

```powershell
cmake --build --preset dev --target cpu_create_lifted cpu_destroy_lifted
ctest --preset dev --no-tests=error -R '^cpu_aot_context_lifecycle$' -V
```

CI compares an independently assembled native create caller and its translation, each paired with the fixture's lifted destroy. To check the package translation from a VS x64 developer shell, set `$loader` to the path of the local FidelityFX loader and run:

```powershell
New-Item -ItemType Directory -Force out/ffx-lifecycle | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_create_context.py $loader out/ffx-lifecycle/create.cpp
./out/pe-env/Scripts/python.exe -B tools/cpu_chained_aot.py $loader ffxDestroyContext out/ffx-lifecycle/destroy.cpp --ffx-destroy
foreach ($unit in 'create','destroy') {
    $prefix = "out/ffx-lifecycle/$unit"
    cl /nologo /O2 /EHsc /LD "/Fo:$prefix.obj" "$prefix.cpp" /link "/OUT:$prefix.dll" "/IMPLIB:$prefix.lib"
    if ($LASTEXITCODE) { throw "Compilation failed for $unit" }
}
./out/pe-env/Scripts/python.exe -B tests/cpu/context_lifecycle.py out/build/dev/cpu_create.dll out/ffx-lifecycle/create.dll out/ffx-lifecycle/destroy.dll
```

### FidelityFX version enumeration

The Windows query returned one upscaler, version ID `0xf5a5ca1e01001001` and name `4.1.1`. The first call requested the count. The second supplied one ID slot and one name slot. Both returned status 0. The descriptor uses AMD's null-context, type-4 version query. [AMD FidelityFX query descriptor](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

`tools/ffx_version_query.py` checks both input DLL hashes, decodes the selected x64 query intervals and the provider's two filter functions, and emits their integer operations and branches as C++. The generated Windows DLL combines both modules' selected paths. Its host binding specializes module resolution to one available upscaler and no frame-generation module. It reconstructs the single provider entry from the initializer's stores, including the full ID and the referenced name. Static storage keeps the returned name valid for the lifetime of the DLL.

On this build, capacity zero reports the available count without writing array elements. ID-only and name-only calls both succeed. Capacity two writes one result and leaves the second slot untouched. A missing frame-generation module returns 4 and an unknown effect returns 2. In both cases the loader clears the count **pointer in the descriptor** and leaves the caller's count value untouched. This entry point covers version enumeration only.

Run these commands in an x64 VS developer shell with the Python development environment available. Set `$loader` and `$upscaler` to the two local input paths.

```powershell
New-Item -ItemType Directory -Force out/ffx-query | Out-Null
./out/pe-env/Scripts/python.exe -B tools/ffx_version_query.py $loader $upscaler out/ffx-query/query.cpp
cl /nologo /O2 /EHsc /LD /Fo:out/ffx-query/query.obj out/ffx-query/query.cpp /link /OUT:out/ffx-query/query.dll /IMPLIB:out/ffx-query/query.lib
./out/pe-env/Scripts/python.exe -B tests/cpu/version_query_check.py out/ffx-query/query.dll query_versions 0xf5a5ca1e01001001 4.1.1
```

The public CTest `cpu_aot_version_query` runs the same output checks against an independent MASM fixture and its translation. The fixture uses a different name and 64-bit ID. The two proprietary inputs and generated code stay local.

The shared integer lowering in this tool emits flat loads and stores, so it rejects FS or GS segment overrides and LOCK prefixes instead of dropping them. A thread-block access such as `gs:[0x30]` would otherwise read absolute address 0x30, and a locked read-modify-write would lose its atomicity. Segment overrides without effect in 64-bit mode and `rep ret` lower like the bare instruction. A host operation that replaces an instruction bypasses the check. `ctest --preset dev --no-tests=error -R '^cpu_lower_prefixes$' -V` checks 11 rejected encodings and 6 accepted controls with synthetic bytes.

A 67h address-size prefix selects 32-bit addressing. The lowering wraps base + index × scale + displacement at 32 bits and zero-extends the result, ignoring the upper halves of the registers as the hardware does. In this mode iced reports a displacement of -8 as `0xfffffff8`, so the wrap is also needed when the upper halves are zero. EIP-relative operands are rejected because the hardware adds the low 32 bits of the runtime RIP, which an RVA binding cannot represent. `cpu_aot_address_size` builds `lea`, load and store forms such as `lea rax, [ecx+edx*8-8]` and `mov [ecx+edx], r8d` from MASM source. It translates them and compares both images with a 32-bit reference over wrapped sums and upper-half noise. The loads and stores use a page committed below 4 GiB, and each store must leave the rest of the page unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_address_size$' -V`.

An immediate SHL, SHR or SAR whose masked count is zero leaves the flags unchanged, but a 32-bit destination is still written and its upper half clears. This covers `shl eax, 0` and `shl eax, 32`, because the count is masked to five bits. The lowering keeps the flags and emits the zero-extending write. A 64-bit destination keeps every bit. `cpu_aot_shift_count` builds these forms from MASM source, together with a nonzero control. It compares the native and translated images with a reference and checks that `sete` after a zero-count shift still observes the preceding compare. Before this change the translation returned `0xdeadbeef12345678` for `shl_zero(0xdeadbeef12345678)` where the hardware returns `0x12345678`. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_shift_count$' -V`.

The shared lowering also handles 32-bit and 64-bit BMI1 bit-manipulation forms (`andn`, `bextr`, `blsi`, `blsmsk`, `blsr`, `tzcnt`), including their zero-extending 32-bit writes and ZF/CF updates. `cpu_aot_bmi1` probes CPUID leaf 7 EBX bit 3 (`BMI1`) before invoking the native fixture and exits 77 (capability skip) on hosts without BMI1; on BMI1-capable hosts it builds all twelve 32-bit and 64-bit exports from MASM source (`tests/cpu/bmi1.asm`), translates them, compares both values and observed ZF/CF flags against the native DLL and an independent reference, and checks that 16-bit `tzcnt` is rejected. Run `cmake --build --preset dev --target cpu_bmi1_lifted` and `ctest --preset dev --no-tests=error -R '^cpu_aot_bmi1$' -V`.

## Recompiling a direct x64 call

The [caller and callee](../tests/cpu/direct.asm) each accept two unsigned 64-bit integers and an output pointer. The [translator](../tools/cpu_call_aot.py) starts at the caller export, follows the signed displacement in its E8 call and reads the target's runtime-function interval. It generates two C++ functions from the linked machine code. MSVC emits a separate x64 DLL with a non-inlined call. The Windows test checks the relative call against the two export addresses in both the input and output DLLs.

```powershell
cmake --build --preset dev --target cpu_call_original_lifted cpu_call_altered_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_direct_call$' -V
```

The caller changes both arguments and keeps its original inputs live across the call. The callee records its arguments and arithmetic result through R8. The caller uses the returned RAX and both live inputs to compute its result. A separate assembly probe seeds physical RBX and RSI and reads them immediately after each invocation. Tests exercise the whole call and the callee in isolation using the same 249 input pairs, including wraparound and high-word differences. They compare the return, three output words, adjacent guards and both register sentinels with the original and independent arithmetic.

A rebuilt callee that increments only its return after storing the result exposes a mismatch for every pair. A register-clobber control checks the probe itself. Targets redirected into the middle of the callee or back to the caller are rejected during translation.

A [second caller](../tests/cpu/repeat_call.asm) calls the same callee twice. Only RBX and RSI survive each call, so it parks the trace pointer in RSI and copies it back to R8 for the second call, whose arguments depend on the first result. The translator decodes 64-bit moves between R8 and the profile registers in both the opcode 8B form that ML64 emits and the opcode 89 form. A repeated direct call to the same target lifts that callee once, and a repeated import call through the same IAT slot binds that import once. It tracks which registers still hold the entry R8 value, so R8 can only be reloaded from such a copy and every store stays inside the trace.

```powershell
cmake --build --preset dev --target cpu_repeat_original_lifted cpu_repeat_altered_lifted cpu_repeat_dropped cpu_repeat_import_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_repeated_call$' -V
```

For the same 249 input pairs, the caller and callee match the original and independent arithmetic (for both the direct-call fixture and the repeated-import `IMPORT_MIX` build). The comparison covers the return, the trace written by the second call, the guards and the RBX/RSI sentinels. With a callee that returns one more, the caller's return and trace change on every pair. Both the original and the lifted DLL contain two rel32 calls to their callee. Replacing both R8 moves with their opcode 89 encodings leaves the translation unchanged. Translation rejects mixed direct/import calls, two distinct IAT slots, and six patched or rebuilt copies:
- one assembled without the R8 reload, as an undefined register;
- the first or the second call redirected into the callee interior, which owns no function interval;
- both calls redirected there;
- both calls redirected back to the caller;
- R8 reloaded from RBX, which holds an input word.

Hand-built callers that combine a direct call and an import call are rejected in either order, as mixed import and direct calls. A caller that calls two different IAT slots is rejected as a call to a second IAT slot.

A [third caller](../tests/cpu/call_pair.asm) calls two different callees, `mix_args` and then `mix_other`, which combines its arguments with XOR so the two calls do not commute. The translator lifts each distinct direct target once, in call order, as `lifted_callee`, `lifted_callee2` and so on. Each target must own a function interval that overlaps neither the caller nor another callee.

```powershell
cmake --build --preset dev --target cpu_pair_original_lifted cpu_pair_swapped_lifted cpu_pair_first_lifted cpu_pair_second_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_call_pair$' -V
```

For the same 249 input pairs, the caller and all four lifted callees match the original and independent arithmetic, with guards and RBX/RSI sentinels. A copy assembled with the two calls swapped follows its own reference and changes all 249 results, and its lifted DLL names `mix_other` first. Copies with only `mix_args` or only `mix_other` returning one more each change the caller's return on every pair. Each original and lifted caller contains exactly one rel32 call to each of its callees. Translation rejects three redirected calls: either call into the other callee's interior, and the second call back to the caller. A copy whose `mix_args` runtime-function entry is extended into `mix_other` is refused earlier, by the range reader, as an invalid runtime-function interval.

Coverage is limited to this integer ABI, its two fixed frame layouts and the decoded register operations. The fixtures exercise two distinct callees at most, and not a caller that returns to an earlier callee after calling another. They exercise the R8 moves only through RSI, and import calls are still limited to one IAT slot per caller. [Windows x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention) · [Import-call follow-up](https://github.com/R0Y00/Xbox2Windows/issues/24)

## Calling another DLL through a named PE import

The source-built caller uses an x64 RIP-relative indirect call (`FF 15 disp32`). The translator computes the import address table (IAT) slot RVA from the end of the instruction and looks up the exact slot in the ordinary import descriptors. The build supplies an explicit binding for the fixture's integer ABI. The DLL name and case-sensitive symbol must match the slot, and the provider must have a local executable export for that name. MSVC compiles the generated caller and links the provider's import library. The test independently checks that the call in each linked caller addresses its own slot for `cpu_import_provider.dll!mix_args`. [PE import address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#import-address-table)

```powershell
cmake --build --preset dev --target cpu_import_original_lifted cpu_import_altered cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_import_call$' -V
```

Each provider configuration runs in a fresh Windows process. The provider records the two arguments it received and its arithmetic result in three output words. For 249 pairs the test compares both callers with independent arithmetic, checks the adjacent guard words and samples RBX and RSI. Replacing the provider with one that increments its return changes the caller's result on every pair and preserves the output writes.

With the provider absent, both callers fail to load with error 126. With a DLL of the expected name that lacks the requested export, both fail with error 127. The translator also diagnoses the missing provider or export before compilation and rejects a call displaced one byte into the IAT slot. The verified profile covers one ordinary named import with two 64-bit integers and an output pointer. [Ordinal-only import follow-up](https://github.com/R0Y00/Xbox2Windows/issues/26)

## Calling an ordinal-only export

The source-built provider exposes only ordinal 77 through a NONAME export. The build binds `#77` with the local import-library symbol `mix_args`. The translator checks the DLL and ordinal from the call's IAT slot against the actual export address table. The test reads both linked callers' import lookup words, checks the ordinal flag and value, and verifies the provider has no exported name for the entry.

```powershell
cmake --build --preset dev --target cpu_ordinal_original_lifted cpu_ordinal_altered cpu_register_probe cpu_import_provider
ctest --preset dev --no-tests=error -R '^cpu_aot_ordinal_import$' -V
```

For 249 input pairs both callers match the provider's observed arguments, guarded writes, register sentinels and expected return. Replacing the provider changes the return on all inputs. Both callers fail to load with error 126 when the DLL is absent and error 182 when the DLL exports the same link name but lacks ordinal 77. The static translator diagnoses a mismatched binding, a missing link name, a missing ordinal and a displacement into the middle of a slot. When the provider is renamed `ws2_32.dll`, pefile names ordinal 77 from its built-in table. The translator still requires the `#77` binding and rejects that name.

The local Bink DLL imports ordinal 1 from XAudio2_9.dll. A read-only check confirmed an executable export at that ordinal in this workstation's System32 provider. The Bink call's argument and result contract remains unverified.

## Mapping an image without LoadLibrary

`tools/pe_map.cpp` maps an x64 PE32+ image into memory it allocates itself, away from the preferred base, and applies its DIR64 base relocations. It binds ordinary and delay imports through a resolver supplied by the host, sets section protections, records the actual base in the mapped headers and registers `.pdata` with `RtlAddFunctionTable`. The original machine code runs unchanged. Header fields are checked against the file and image size before use. It then calls TLS callbacks and the entry point of a DLL; `pe_map_run` starts an EXE. TLS data templates are rejected. [Issue #46](https://github.com/R0Y00/Xbox2Windows/issues/46)

```powershell
cmake --build --preset dev --target pe_map cpu_reloc_original cpu_import_original_lifted cpu_ordinal_original_lifted cpu_import_altered cpu_ordinal_altered cpu_register_probe cpu_two_imports
ctest --preset dev --no-tests=error -R '^cpu_native_map$' -V
```

The test maps the named and ordinal import fixtures, `reloc_route` and a damaged copy of the register probe. In `reloc_route`, `call_mix` reaches the provider through an absolute code pointer in `.data`, which gives the image exactly one DIR64 relocation; the test checks that the mapped pointer equals its linked value plus the load delta. The two import fixtures have no relocation entries. For every image the test checks the following:

- the base differs from the preferred base and is recorded in the mapped headers;
- the IAT slot holds the export the resolver returned;
- `RtlLookupFunctionEntry` finds `call_mix` in the registered table until the image is released.

The mapped `call_mix` is compared over 249 input pairs on arguments, guarded writes, RBX/RSI sentinels and return value. It matches the reference and the same image loaded by Windows, and for the import fixtures it also matches the lifted DLL. If the import is bound to the altered provider instead of the linked one, only the return changes. An unbound import is rejected with its DLL and symbol or ordinal. Copies patched to have an entry point outside any executable section or an import directory that runs past the image are rejected before any import is requested. Like the Windows loader, `pe_map` finds the import table by its address and walks it to the null descriptor without using the directory `Size`; each descriptor is bounds-checked against the image. `two_imports` imports `mix_args` from the named provider and `probe` from the register probe. With its import directory `Size` left at 60 or patched to 0, 19 or 20 (ending after the first descriptor), `LoadLibraryExW` and `pe_map` both bind both IAT slots; the test only reads the slots and runs nothing in the image. The Windows loader does not read the delay-import directory at all: copies of the named fixture with an empty table, an address of 0, an address past the image or a descriptor naming a DLL all load. `pe_map` maps the first two, rejects a directory outside the image as a format error and one that lists a DLL as unsupported. In a copy of the register probe with the `clobber_control` export address slot zeroed, that name no longer resolves while `probe` still does.

`cpu_native_seh` checks exception dispatch through a mapped image. In `seh_route`, `guarded_load(address, mode, trace)` names its own exception handler in its unwind data. The handler resumes an access violation raised by the load instruction at a recovery label with a fixed value in RAX, and declines every other exception. The cases run in one child process for the Windows-loaded original and another for the mapped copy, so a broken unwind crashes only the child and is reported as a failed check:

- a valid load returns the value;
- a load from address zero is caught by the image's own handler and returns the fixed value, with RBX and RSI restored;
- an illegal instruction, reached after the first trace write, is declined and unwinds through the mapped frame to the ctypes caller, which reports exception code `0xc000001d`;
- a later valid call still succeeds.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_seh$' -V
```

`cpu_native_section` compares every section of the register probe after `LoadLibraryExW` and after `pe_map`, byte for byte, on the original and on copies with one section header patched. The probe has no entry point and no imports, so the Windows load runs none of its code. On Windows 11 build 26300.9974, the loader reads section data in whole `0x200`-byte file sectors: it rounds `PointerToRawData` down and the end of the raw data, `PointerToRawData + SizeOfRawData`, up to a multiple of `0x200`. It copies that range up to the section's virtual size, and bytes past the end of the file stay zero. `pe_map` now reads the same range. The test covers a start inside a sector, a start in the first sector, which reads the headers from offset 0, a raw size below one sector and raw data that crosses a sector boundary. In each case Windows differs from an exact read of `SizeOfRawData` bytes at `PointerToRawData`. A last case shortens the file so that the rounded end of `.rsrc` lies past it.

A `PointerToRawData` of zero is the exception: Windows leaves that section zero-filled even when `SizeOfRawData` is set, and so does `pe_map`. The rounding cases patch `.rdata`, because in `.rsrc` a rounded start can put other data where the resource directory should be, and `LoadLibraryExW` then fails with `ERROR_BAD_EXE_FORMAT`. These results were measured with a `FileAlignment` of `0x200` and `0x400`. With `0x1000`, Windows filled more of a section whose raw size is below one sector than the rounded sector end allows, which remains open.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_section$' -V
```


### TLS callbacks and the entry point

`tls_route` has an entry point and a TLS directory whose callback list holds one routine and whose data template is empty. Both routines report each call to `cpu_tls_sink.dll`, which outlives the image, so calls made during release stay visible. `cpu_native_tls` loads the image with `LoadLibraryW` and maps it with `pe_map`, each in its own child process, and compares the process notifications:

- Attach: with both loaders the TLS callback runs before the entry point, each with `DLL_PROCESS_ATTACH`, the module base and a null reserved argument.
- Detach: on Windows 11 build 26300, `FreeLibrary` called the TLS callback first and then the entry point, both with `DLL_PROCESS_DETACH`. `pe_map_release` follows that measured order. No Microsoft documentation specifies it.
- Failed attach: in `cpu_tls_refuse` the entry point returns FALSE. Windows then sends a full detach in the same order, and `LoadLibraryW` fails with error 1114. In the same case `pe_map` sends the same detach and returns `MAP_INIT_FAILED`.

Like the Windows loader, `pe_map` finds the TLS directory by its address and does not use its `Size`. On Windows 11 build 26200, copies with `Size` 0, 1 or 39 still run the callback before the entry point, under both loaders; with the address set to 0 only the entry point runs; a directory past the end of the image fails `LoadLibrary` with error 998 and `pe_map` with "TLS directory outside image". Between attach and release the test starts and joins one thread whose start routine is `Sleep`. On that thread both loaders call the TLS callback and then the entry point with `DLL_THREAD_ATTACH`, then the same pair with `DLL_THREAD_DETACH`, all with a null reserved argument. `pe_map.dll` is itself loaded by Windows, and its `DllMain` forwards these notifications to every mapped image: attach in mapping order and detach in reverse, which is what two `LoadLibraryW` copies receive on Windows 11 build 26300. The sink records the calling thread, so notifications from unrelated threads in the process are ignored. A thread created between an image's process attach and its registration receives the detach without the attach, as a thread that existed before mapping does. Each mapped image holds one reference for its registration and one per thread notification in flight. `DllMain` takes those references under the registry lock and calls the guest callbacks after releasing it, so a callback may map or release any image, its own included. `pe_map_release` unregisters the image and returns 1 at once. The image's process detach and unmapping run when its last reference goes, inside `pe_map_release` or on the thread that finishes the last notification, and happen exactly once. A notification is admitted when its thread takes the image's reference under the registry lock, and none is admitted once release has unregistered the image. An admitted notification may still run after release returns; it skips an image already marked released before calling it, but one whose callback has started finishes, so an image whose TLS callback releases it still has its entry point called for that thread attach. `cpu_native_lifetime` compares the two-image order with two native loads, and releases an image from inside a thread-attach callback, both another image and the image itself, through a hook armed for that thread only. A second release returns 0. Null, an interior address, a foreign `PAGE_NOACCESS` page and a foreign read-write buffer also return 0. The buffer keeps its content and protection, and the unreadable page shows that release does not read its argument. Without a notification in flight, release finishes the process detach, the removal of the image's `.pdata` table and the unmapping before it returns: `RtlLookupFunctionEntry` finds the entry point's function before the release and nothing after it, and the base is no longer committed. In 50 rounds, eight native threads spin on a shared flag and then release the same image at once; each round has exactly one 1 and one process detach. A stale pointer cannot identify an earlier instance: once the address is reused by a new mapping, releasing it releases that new mapping. When another thread releases an image whose TLS callback is blocked inside a thread attach, release returns 1 at once while the unwind entry is still present and no process detach has run. Once the callback returns, the entry point still receives that attach, the worker performs the detach and the table removal once, and the thread detach is not delivered. Damaged copies are rejected before any notification: one with a nonzero `SizeOfZeroFill` as TLS data, one with the entry point moved into `.data`, and one with its TLS callback pointing into `.data`. The entry point and callbacks must lie in an executable section. The TLS directory and each callback slot are read after section protections are applied, so `pe_map` checks the live page protection with `VirtualQuery` before every such read, on attach and on release. In `tls_route` the callback list has its own read-only `.tlscb` section. Copies with `.data` or `.tlscb` made inaccessible are rejected as "TLS directory not readable" or "TLS callback list not readable". A list made `PAGE_NOACCESS` after mapping is skipped on release while the entry point is still detached. `pe_map_export` applies the same rule to the export directory, the address and name tables and each name string, and accepts an export target on an execute-only page; a copy whose export section is mapped without read access resolves no export instead of returning a pointer into it. A list longer than 256 callbacks is rejected as unsupported. With `IMAGE_FILE_DLL` cleared, with or without an entry point, `LoadLibraryW` loads and frees the copy without calling the TLS callback or the entry point, and `pe_map` does the same. `pe_map` still applies the executable-section checks to such an image, where Windows would load it and call nothing. The callback list collected before attach is reused for the detach that follows a failed attach. The list is re-read on release; if it was overwritten with a non-executable address or made unreadable after mapping, only the entry point is detached. Implicit TLS data would need a slot in each thread's `ThreadLocalStoragePointer` array, which ntdll owns. `pe_map` therefore leaves `AddressOfIndex` as linked. In the test process, Windows wrote index 8 for this image even though its template is empty.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_tls$' -V
```

### Starting a mapped EXE

`pe_map_image` maps an EXE without notifying it, as `LoadLibrary` does. `pe_map_run(mapped, &exit_code)` starts it: it calls the TLS callbacks in table order with `DLL_PROCESS_ATTACH` and a null reserved argument, then the entry point on the calling thread, and stores the entry point's return value in `exit_code`. It returns 0 and runs nothing for an image with `IMAGE_FILE_DLL` set, for an image without an entry point and for an unreadable callback list. `mapped` must be a live base returned by `pe_map_image`; the headers are read without validation, as in `pe_map_export`. The entry point and callbacks are arbitrary code running in the host process on the calling thread, so an image that crashes, hangs or calls `ExitProcess` takes the host with it; run unknown images in a child process, as the test does. Start each mapping at most once. The caller keeps the mapping live until the call returns and prevents any thread, TLS callback or entry point from starting or releasing the same mapping during that call. The PEB argument that Windows passes to an EXE's entry point is not forwarded, and no process-detach notification follows, because the host process goes on running.

`cpu_exe_start_original` is a source-built EXE without a C runtime. Its TLS callback and its entry point each append one base-4 digit to a log, then the entry point calls the named import `cpu_import_provider!mix_args(5, 3, out)`, which returns 13, and returns `(log << 8) | 13`. A process launch of the EXE makes that value its exit code. `cpu_native_exe_start` compares the launch with `pe_map_image` and `pe_map_run` in a child process:

- Original: both give `0x60d`: the callback (digit 1, reserved null) ran before the entry point (digit 2), and the import returned 13.
- `cpu_exe_start_no_tls` has no TLS directory. Both give `0x20d`, so the log reflects the callback.
- The original bound to `cpu_import_altered`, whose `mix_args` returns one more, gives `0x60e` under `pe_map`.
- With `IMAGE_FILE_DLL` set in a copy, `pe_map_run` returns 0 and leaves the exit code untouched.
- With the entry point moved into `.data` in a copy, the Windows launch faults with `0xc0000005`, while `pe_map_image` refuses it as "entry point outside executable section" and nothing runs.

As a one-off manual check (not in CI), I removed the callback loop from `pe_map_run`; the test then failed with `mapped original 0x20d != native 0x60d`.

This shows one EXE with an ordered start log and an import bound through the resolver. It does not run a C runtime startup, which would call `ExitProcess` in the host. It does not cover implicit TLS data, a second thread's TLS callbacks, static initializers in imported DLLs, `GetModuleHandle(NULL)` (which still returns the host) or the PEB. Those remain untested or unsupported. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_exe_start_original cpu_exe_start_no_tls cpu_import_provider cpu_import_altered
ctest --preset dev --no-tests=error -R '^cpu_native_exe_start$' -V
```

### Starting an EXE with two DLLs

`cpu_startup_exe` and two DLLs, all built without a C runtime, import each other in a known order. `cpu_startup_a` has no imports and keeps an event log and a value set by its initializer (`7`). `cpu_startup_b` imports `cpu_startup_a`, and its initializer publishes `a_value() * 2`, so it reads zero if it runs first. The EXE imports both. These initializers are the DLLs' `DLL_PROCESS_ATTACH` entry points, called by the loader, not C++ static constructors. The EXE's TLS callback logs `t` and its entry point logs `e`. The entry point then runs two worker threads one after the other and joins each with a 30 s limit. Every thread keeps its own value in a `TlsAlloc` slot (`1` in the main thread, `2` and `3` in the workers). That is explicit TLS through kernel32, not implicit `__declspec(thread)` data. The entry point writes the log to standard output and returns `b_value() | first worker << 8 | second worker << 16 | main << 24` as its exit code.

`cpu_native_startup` launches the EXE with the Windows loader in a directory holding the two DLLs, and starts it with `pe_map_run` in a child process. The harness reads the import tables, orders the images dependencies first, maps each DLL, serves the imports of the DLLs with `pe_map_export` on the mapped images and the kernel32 imports with the host. `pe_map` does the mapping, relocation, section protection, each DLL's attach notification and the start of the EXE; it does not read import tables to find dependencies. On Windows 11 build 26300:

```text
native: LOG:abtew2w3m1 exit 0x103020e
mapped: LOG:abtew2w3m1 exit 0x103020e
```

- The logs, exit codes, standard output and standard error (empty) are identical. Under both loaders `a` and `b` ran once and in dependency order, the TLS callback ran after the DLL initializers and before the entry point, the entry point ran once, and `b_value()` was `14`. The three threads read their own `TlsAlloc` values.
- Without B's initializer (`cpu_startup_b_noinit` under B's name) both loaders give `LOG:atew2w3m1` and exit code `0x1030200`. With another A under A's name (`cpu_startup_a_alt`, whose value is 9) both give the original log and `0x1030212`. Both controls differ from the original under both loaders.
- Mapping the EXE before its DLLs fails with `unresolved import cpu_startup_b.dll!b_value`.
- Thread notifications are not part of the comparison: the fixture does not count the `DLL_THREAD_ATTACH` events of the workers, because `pe_map` does not forward them to mapped images.

Not covered: `DLL_PROCESS_DETACH` ordering at exit, DLLs built with the C runtime (C++ constructors), implicit TLS and `GetModuleHandle(NULL)`. Measured on Windows 11 build 26300. [Issue #165](https://github.com/R0Y00/Xbox2Windows/issues/165)

```powershell
cmake --build --preset dev --target pe_map cpu_startup_exe cpu_startup_a cpu_startup_a_alt cpu_startup_b cpu_startup_b_noinit
ctest --preset dev --no-tests=error -R '^cpu_native_startup$' -V
```

### Lifecycle of two DLLs with the static C runtime

`cpu_life_a` and `cpu_life_b` are built with the static MSVC runtime (`/MT`, linked without `/INCREMENTAL`, so the PE entry point is the runtime's `_DllMainCRTStartup` and not an incremental-link thunk). A real global constructor runs before `DllMain(DLL_PROCESS_ATTACH)` and a real global destructor after `DllMain(DLL_PROCESS_DETACH)`. Nothing calls them by hand. B imports A. A's constructor sets its state to 7. B's constructor stores `a_value() * 2`, and its destructor reads A's state again. `b_via_a()` returns `a_value() + b_value()` at the time of the call. Every constructor, destructor and `DllMain` call logs a token into `cpu_life_sink.dll`, a small CRT-free DLL that the test host loads first and keeps for the whole child process, so it is valid during every destruction and sees the last destructor. `DllMain` only calls into the sink, which takes no lock and needs nothing from the loader.

Each constructor also records its callers with `RtlCaptureContext`, `RtlLookupFunctionEntry` and `RtlVirtualUnwind`. The test reads the entry-point function from `.pdata`, follows its closing `jmp rel32` to the function that dispatches the process attach and detach (this is why the entry point itself is not on the stack), and requires that function on the constructor's stack with the next frame outside the image. That frame is the loader's under Windows and `pe_map`'s under `pe_map`. `RtlCaptureStackBackTrace` is not used: on an image that `pe_map` mapped it returns only the first frame.

`cpu_native_crt_lifecycle` runs one scenario in fresh child processes with a 30 s timeout: through `LoadLibraryExW` and `FreeLibrary`, where the loader finds A as B's dependency and decides every order, and through `pe_map_image`, `pe_map_export` and `pe_map_release`. For `pe_map` the harness orders the images from the import tables, maps A before B, serves B's import of A with `pe_map_export` and releases B before A; `pe_map` itself keeps no dependency graph. The scenario loads B, calls the exports, changes A's state with `a_set(99)`, unloads, reloads, unloads, and then runs two more load and unload cycles. Three fresh processes per loader must report identical stages. On Windows 11 build 26300 both loaders give identical events, values and residency at every stage:

```text
load:           A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_value 14, b_via_a 21, a_ctor_runs 1
a_set(99):      (no events)                                 a_value 99, b_via_a 113
unload:         B:detach, B:dtor a=9, A:detach, A:dtor      A and B no longer mapped
reload:         A:ctor, A:attach, B:ctor, B:attach          a_value 7, b_via_a 21, a_ctor_runs 1
unload again:   B:detach, B:dtor a=7, A:detach, A:dtor      A and B no longer mapped
cycles 3, 4:    the load and unload lines above
```

The reload starts from fresh state (7, not 99) and runs each constructor once more. B's destructor still sees A alive, with A's state at that time (`a=9`, then `a=7`). Residency is `GetModuleHandleW` for Windows and `VirtualQuery` on the mapped base right after the releases for `pe_map`.

- Without A's constructor (`cpu_life_a_noctor`) both loaders log `A:attach, B:ctor, B:attach`, every value is 0 and A's constructor is not among the callers. Without B's destructor (`cpu_life_b_nodtor`) both log `B:detach, A:detach, A:dtor` on unload. Both controls differ from the original in the events under both loaders.
- Base addresses: Windows loaded B at the same address in all three processes, as the system randomizes a DLL's base once per boot. `pe_map` mapped it at a different address every time and never at the preferred base, so the relocation path ran on every mapping.
- Responsibilities: Windows counts references. A second `LoadLibrary` of B returns the same module and runs nothing, and A stays loaded while B or the harness holds it. `pe_map` keeps no module table and no reference counts. Mapping B twice gives a second image that runs its constructor again. The order of mapping and release and the import of A are the harness's work.
- Mutation checks, not in CI: making `pe_map_release` skip the process detach, or not free the image, each made `cpu_native_crt_lifecycle` fail.
- Not covered here: a double release, null and foreign or interior pointers, which `cpu_native_lifetime` already checks for `pe_map_release` (it returns 1 once and 0 otherwise); and releasing A before B, which would touch released memory. Neither this test nor the harness depends on freed memory, apart from asking `VirtualQuery` about the released range right after the release.

```powershell
cmake --build --preset dev --target pe_map cpu_life_sink cpu_life_a cpu_life_a_noctor cpu_life_b cpu_life_b_nodtor
ctest --preset dev --no-tests=error -R '^cpu_native_crt_lifecycle$' -V
```

### An empty relocation directory away from the preferred base

`cpu_import_original` contains no absolute addresses. Its base-relocation directory has Size 0 and `IMAGE_FILE_RELOCS_STRIPPED` is clear. The issue used `cpu_register_probe`, which has the same properties; this test uses `cpu_import_original` because it exports `call_mix`, so a call can be checked after loading, and the register probe is the harness.

`cpu_native_reloc_free` runs each case in its own child process. It reserves the preferred base (`0x180000000`) for `SizeOfImage` and checks with `VirtualQuery` that the whole range is held by that reservation. It then loads the image with `LoadLibraryExW` and maps it with `pe_map`. A second copy has `IMAGE_DLLCHARACTERISTICS_DYNAMIC_BASE` cleared.

The discriminating case is Windows with the `DYNAMIC_BASE`-cleared copy. With nothing reserved, Windows loads that copy at `0x180000000`. With the base reserved, it loads it elsewhere and `call_mix` matches the reference on all 249 input pairs. The as-built copy also moves under Windows, but ASLR would move it anyway. `pe_map` always allocates away from `ImageBase`, so for it the reservation only shows that an empty relocation directory is accepted at another base and that the image runs there.

The bases vary between runs, so the test checks only that they differ from the preferred base. With the base reserved, a copy that sets `IMAGE_FILE_RELOCS_STRIPPED` fails in `LoadLibraryExW` with error 487 (`ERROR_INVALID_ADDRESS`) and is rejected by `pe_map` as "relocations stripped"; `pe_map` would reject it with or without the reservation.

`pe_map` therefore does not reject an empty relocation directory when it maps away from `ImageBase`, because Windows loads such images. An image that contains absolute addresses but omits its relocations is malformed and would fail at run time under either loader. Measured on Windows 11 build 26300. [Issue #77](https://github.com/R0Y00/Xbox2Windows/issues/77)

```powershell
cmake --build --preset dev --target pe_map cpu_import_original cpu_import_provider cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_native_reloc_free$' -V
```

### Mutated images

`cpu_native_mutation` applies a fixed list of single-field edits to the header, the first section header, and the export, import and base-relocation directories of `cpu_register_probe` and `cpu_import_original`, plus three truncations. Each case runs in its own process and must print either `MAP_OK` or a named `MapStatus` with a detail string; a crash or a timeout fails the case. Across 46 cases the outcomes were 26 `format`, 16 `ok` and 4 `relocation`, with no crash. A three-line mapper that dereferences null makes the parent report `child exited 0xc0000005`, so the harness detects the failure mode it covers.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_mutation$' -V
```

## Switch dispatch through jump tables

Compilers lower a dense `switch` to a bounds check and an indirect `JMP` through a table of 32-bit entries. The [jump-table translator](../tools/cpu_switch_aot.py) recognizes the two x64 layouts in common use. MSVC stores image-relative entries and loads them through a register holding `__ImageBase`. LLVM stores signed offsets from the table and adds the table address. The translator reads the table from the PE, then emits a C++ `switch` whose cases jump to labels at the decoded targets. The host compiler builds its own dispatch from that switch.

```powershell
cmake --build --preset dev --target cpu_switch_original_lifted cpu_switch_swapped_lifted cpu_register_probe
ctest --preset dev --no-tests=error -R '^cpu_aot_jump_tables$' -V
```

The decoder starts at the export and follows branches, fallthrough and table entries, so it never decodes the table bytes that MSVC places after a function's code. An indirect jump is accepted only in the sequence `load entry; add base; jmp`, reached by the fallthrough of an unsigned `JA` or `JAE` against an immediate. The table length comes from that bound. A must-agree constant pass over the control-flow graph gives the base register's RIP-relative value, which also covers a base loaded once outside a loop. Value numbering over the single-entry block that contains the bound shows that the index register holds the compared value, zero- or sign-extended from 32 bits. Every table entry must be the first byte of a decoded instruction inside the function, and the table must not overlap any decoded instruction. Flags are modelled only between a producer (`CMP`, `TEST`, `ADD`, `SUB`, `AND`, `OR`, `XOR`, `INC` or `DEC`) and its consumers: a conditional jump, `SETcc` or `CMOVcc`. Producer and consumer must sit in one straight-line run that no edge enters; only instructions that leave the flags unchanged (moves, `LEA`, `NOT`, `SETcc`, `CMOVcc`) may lie between them. The overflow and parity conditions are rejected. A forward pass records how many low bytes of each register (0, 1, 2 or 8) every path defines, and rejects a read of more bytes than that. Writes to AH, BH, CH and DH, 16-bit `CMOVcc` and calls are rejected. Nonvolatile registers and stack operations are accepted only within the fixed frames described below.

The supported functions take three integer words in RCX, RDX and R8 and return RAX, of which only the low bytes described below are compared. An entry can instead be translated with a fourth integer word in R9, as described below. Stack arguments (the fifth and later) and floating-point arguments are not supported. A leaf function without a `.pdata` entry is bounded by its executable section. Its walk may not enter an interval owned by another function's unwind entry.

The [source fixture](../tests/cpu/switch.asm) exports six functions. `select_rva` uses the MSVC layout, and `select_rel` computes the same function through the LLVM layout. Both have seven cases, including two selectors that share a target, a case that falls through into the next, and a default path. `run_ops` runs a byte program in a loop and dispatches every byte through a five-entry table. It stops at the first opcode above 4. Through the register probe, the test compares each original export with the matching lifted function, a reference written in Python, and RBX/RSI sentinels. It runs 600 selector and operand pairs per layout and 138 programs. Half of the pairs carry nonzero upper register halves that must not reach the table index. A control assembles the same source with two entries swapped in each table, and its translation must follow the swapped reference. `shift_narrow` dispatches through a six-entry LLVM-layout table to byte and word `SHL`, `SHR` and `SAR` of a memory destination by `CL`. x86 masks the count to five bits for 8- and 16-bit operands as well, so counts of 8 to 31 still shift (a byte holding 1 shifted left by 8 becomes 0). The translator performs these shifts in 64 bits and truncates the stored result; 4,608 inputs, covering every count from 0 to 31 and count bytes above 31, match the original and the reference. `flag_select` dispatches through an eight-entry LLVM-layout table to `SETL`, `SETBE`, `SETG` into a memory byte, `SETNE` after a 16-bit compare, and `CMOVA`, `CMOVNS` (64-bit, after `TEST`), `CMOVGE` from memory with a `MOV` between compare and consumer, and `CMOVE` after `TEST` with an immediate. It compares EDX with bits 32 to 63 of RDX and returns RAX whole. A 32-bit `CMOVcc` therefore shows that it clears bits 32 to 63 even when the condition is false, and a byte `SETcc` shows that it keeps them. The translation reads a memory source of `CMOVcc` on both outcomes, as the processor does. 4,000 inputs at the signed, unsigned and 16-bit boundaries must match the original and the reference. The swapped-table build exchanges the `SETL` and `CMOVA` entries and must follow the swapped reference. `cmov_read` executes `CMOVNE` after `CMP ECX, ECX`, which never moves but still reads its memory source: called with a null R8 it must fault in the original and in the translation. The translator therefore reads a `CMOVcc` memory source through a `volatile` load; with an ordinary load, an optimized MSVC Release build drops the read and the null call returns.

On a copy of the linked DLL, the test patches the code, the table or `.pdata`, and expects each change below to be rejected:

- the bound jump removed or made signed;
- a sign extension that no longer comes from the compared register;
- a table entry into an instruction interior or into the neighbouring function;
- a table placed over code, or a second entry into the dispatch block;
- a base that is not the image base, or that does not come from a RIP-relative `LEA`;
- an undefined or nonvolatile register, or an unsupported instruction;
- a leaf whose walk reaches another function's unwind interval;
- in `flag_select`, `SETO`, a `SETcc` into `AH`, a flag-writing `NEG` between a compare and its `CMOVGE`, and a second entry that reaches a `SETBE` with another path's flags.

When `.pdata` no longer covers `select_rva`, the translation must stay unchanged.

On Linux, the fixture was assembled and linked with LLVM (`llvm-mc`, `lld-link`) from an equivalent GNU-syntax copy, because `llvm-ml` does not accept `IMAGEREL`. The original machine code ran in-process through an `ms_abi` call. It matched the lifted C++ built with clang on every input, for both the original and the swapped tables. The same translator also handled C switch statements compiled by clang for `x86_64-pc-windows-msvc` at `-O1` and `-O2`. Those builds have no `.pdata` for the leaves, and the loop's table base is hoisted. The generated sources compile without warnings under clang-cl `/W4 /WX`. For `flag_select`, the same Linux run matched the original and the reference on all 4,000 inputs, and the swapped table changed 762 results. The translator before this change rejected `flag_select` at its first `SETL`. Clang output at `-O1` and `-O2` for small C comparisons, selects and clamps (`SETL`, `SETE` into `R8B`, 32- and 64-bit `CMOVcc`) matched its translation on 25,000 calls per level. On Windows 11 with MSVC 14.44 and ML64, `cpu_aot_jump_tables` passed before `flag_select` was added: 600 selector pairs per table layout and 138 programs match, the swapped tables change 60, 60 and 99 results, and all 13 rejection controls and the leaf-without-unwind translation pass. The table labels are declared with `LABEL DWORD`, because ML64 rejects `IMAGEREL` of a code label inside a memory operand.

The jump-table translator wraps a 67h (32-bit) effective address at 32 bits and zero-extends it, as the hardware does. It rejects EIP- and ESP-based operands. `cpu_aot_switch_addr32` builds `lea rax, [ecx+1]`, `lea rax, [ecx-8]`, `mov eax, [ecx-8]` and `mov [ecx+8], edx` from separate MASM source. It compares the native and translated images over wrapped sums and upper-half register noise, using a page committed below 4 GiB for the load and store. Before this change the translation returned `0x100000000` for `lea_wrap(0xffffffff)` where the hardware returns 0. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_switch_addr32$' -V`.

The generated C++ follows address order, so a function that continues in code below its entry starts with a jump to the entry. A leaf without unwind data can reach such code through a jump, because it may use any unowned bytes of its section. Before this jump was emitted, the translation of such a function began at its lowest instruction and never ran the entry. `cpu_aot_entry_order` builds `inc_square`, which increments RCX and jumps to `square_tail` below it, and `pick_tail`, which branches there on a comparison. Both match the native exports on return, guarded trace, RBX and RSI over 249 input pairs. The control export `square_tail`, whose entry is its lowest instruction, still starts at the entry. A function whose chained unwind range lies below its entry takes the same path and has no fixture yet.

An 8- or 16-bit register write leaves the other bytes of the register unchanged, as on x86. The generated C++ merges the new byte or word into the 64-bit variable, which is declared with an initial value. The defined byte count becomes the larger of the old count and the write width, and a 32- or 64-bit write defines all eight bytes. A `SETcc` into a byte register is such a write, so it no longer needs the rest of the register to be defined; after `setl al` only AL is defined. A 16-bit `CMOVcc`, which would merge into the low word only when its condition holds, stays rejected. A 16-bit `IMUL` multiplies in `std::uint32_t`, because two `uint16_t` factors are promoted to `int` and `0xffff * 0xffff` overflows it. `RET` and `RET 0` (`C2 00 00`, which MSVC emits) no longer read RAX. The return width is the number of consecutive low RAX bytes that are definitely defined at every reachable `RET` or `RET 0`: 0, 1, 2 or 8, the lower bound over joins and over returns. It counts defined bytes and does not establish a source-language type. When no `RET` is reachable the width is `None` and the output says `no reachable normal return`, which describes the reachable control flow and makes no termination claim. Microsoft's [x64 calling convention](https://learn.microsoft.com/en-us/cpp/build/x64-calling-convention#return-values) states that the state of unused bits in the value returned in RAX is undefined. The generated function still returns a 64-bit `word`, callers need no width declaration and may use only those bytes, and a comment states the width unless it is 8. The upper bytes that the generator initializes carry no guest result. A `RET` reached while RAX may hold a guest address, for example from a RIP-relative `LEA`, is rejected, so such a function is not reported with a narrower width.

`cpu_aot_narrow_returns` builds [`narrow.asm`](../tests/cpu/narrow.asm) and translates thirteen exports: byte merges into defined registers, word `ADD`, `SUB`, `IMUL` and `MOVZX`, byte `SHL` and `SAR` and word `SHR` and `SAR` by `CL`, byte `INC`, `NOT` and `DEC` and word `NEG`, a comparison that writes only AL, a function that writes EAX on one path and only AL on the other, a copy that writes only AX, `MOVZX` from AL, `SETL` with a byte `MOV` between the compare and the `SETL`, `SETB` after a byte compare followed by `MOVZX`, two functions that store a word and write no RAX byte, one ending in `RET 0`, and `self_loop`, a loop that branches to itself. `self_loop` is translated and inspected and is never called: it must report `None` and `no reachable normal return`, while the storing function with a reachable `RET` must report width 0. Through the register probe, each original and translated export is compared with a Python reference over the direct-call input pairs; the shift export uses counts 0 to 31 and the count bytes 0x21, 0x3f, 0x80 and 0xff under nonzero upper halves. The original must match the reference in all of RAX, the guarded trace and RBX/RSI; the translation must match in the low return-width bytes of RAX and in the rest. The probe enters each function with RAX holding the function's own address, and the original's RAX bytes above the return width still hold that value on every path that leaves them unwritten. The test also checks the widths reported by the translator. Seven controls must be rejected: `mov al, dl` patched to `mov ah, dl`, `movzx eax, al` patched to `add eax, 0` after `mov al, dl` and after `setb al`, `RET 0` patched to `RET 8`, a join where one path defines only AL before `add eax, 1`, `cmovb ax, dx`, and `lea rax, byte_merge; ret`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 3,819 inputs matched, the two inspected exports reported `None` and 0, and all seven controls were rejected. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_narrow_returns$' -V`.

Whoever invokes the translator can give an entry a fourth integer argument in R9 by listing it as `name:4` in the export list of the command line, of `translate` or of the CMake function `cpu_translation`. `name` and `name:3` keep three arguments, and any other count is rejected with `unsupported argument count`. The count is that caller's choice. The translator does not derive it from the machine code, which does not show how many parameters the source function declares. On a four-argument entry R9 is defined at entry and the generated function takes a fourth `word r9` parameter, so a read of R9 is accepted whether or not the source function declares a fourth parameter. A three-argument entry, the default, translates as it did before this option: R9 is undefined at entry, a read before a write is rejected with `undefined register r9`, and a write followed by a read is accepted with `r9` declared as a local. The host must declare and pass the same number of arguments as the entry was translated with. A four-argument entry that never references R9 has an unused parameter, which `/W4` reports.

`cpu_aot_fourth_argument` builds [`fourth_arg.asm`](../tests/cpu/fourth_arg.asm) and translates `r9_mix:4,r9_loop:4,no_r9:4,r9_scratch`. `r9_mix` computes `lea rax, [rcx+r9*2]; sub rax, rdx` and stores R9. `r9_loop` counts additions down in R9D through `CMP`/`JA`, `TEST`/`JZ` and `DEC`/`JNZ`, and stores R9 as it leaves it. `no_r9` does not touch R9. `r9_scratch`, a three-argument entry, writes R9B before any read of R9, reads only R9B and then overwrites R9. The register probe gains `probe4`, which passes its fifth argument to the function in R9 and samples RBX/RSI through its sixth. The test checks that the built source is the translation of that list. It calls each four-argument entry through `probe4` and the three-argument entry through `probe`, and compares native and translated calls with a Python reference on the return value, the guarded trace, RBX and RSI. The four-argument entries get the direct-call input pairs crossed with ten fourth arguments, among them 0, 1, 64, 65, 2^32 - 1, 2^32, 2^63 and all ones, and for `no_r9` the fourth argument must not change any result. As a control for an existing three-argument entry, `byte_merge` from `narrow.asm`, which writes R9 before reading it, keeps its three-parameter signature and its `r9` local and still matches through `probe`. `r9_mix` must be rejected with `undefined register r9` when translated with three arguments and accepted as `r9_mix:4`. The counts `5` and `x` must be rejected, and so must a copy of the image with the index register of `r9_mix` patched from R9 to R10, with `undefined register r10`. With MSVC 14.52 `cl`, `link` and `ml64` run under Wine on Linux, 7,719 calls per side matched and every control passed. The Windows job runs the same test. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_fourth_argument$' -V`.

A call-free function may keep a fixed stack frame ([#317](https://github.com/R0Y00/Xbox2Windows/issues/317), step 4). The translator tracks RSP as a constant offset from its entry value. Only `PUSH` and `POP` of a 64-bit register and `SUB` or `ADD` of RSP with an immediate change it. Paths must meet with the same offset, and every `RET` must see offset 0; any other write to RSP is rejected. `[RSP+disp]` loads and stores with no index become offsets into one `alignas(16)` byte array per invocation. The array runs from the deepest offset to the end of the caller's 32-byte home area, and `frame[0]` stands for an offset that is 8 mod 16, as entry RSP is after a `CALL`, so each slot keeps its native alignment. An access must lie at or above the current RSP, and either below the return address or inside the home area. The return-address slot, stack arguments, RSP as an index and any other use of a frame address (`LEA`, a register copy of RSP) are rejected, so no frame address leaves the function. The definedness pass also tracks each frame byte per path: a read needs every byte it reads written on every incoming path, and home-area bytes start undefined. The nonvolatile registers (RBX, RBP, RSI, RDI, R12–R15) start holding the caller's value, which the translation does not reproduce. A `PUSH` of such a register saves it, and a `POP` of that slot into the same register restores it. Reading a caller's value other than to save it, reading a save slot other than through its restore, and reaching a `RET` with any nonvolatile register not restored are rejected. The generated C++ keeps each register as a local; the host compiler preserves the physical nonvolatile registers itself.

`cpu_aot_stack_frames` builds [`frames.asm`](../tests/cpu/frames.asm) and translates five exports. `save_nonvolatile` uses RBX and RSI as scratch between `PUSH` and `POP`. `spill_reload` stores a qword, two dwords, a word and two bytes into a 28h-byte allocation and reloads them as a qword and a dword after clobbering the sources. `home_spill` spills RCX and RDX into the home area with no allocation of its own. `join_frame` writes the same slots with different values on two paths before they join. `loop_counter` keeps a counter and an accumulator in the frame across a loop's back edge. Through the register probe, the original must match a Python reference, and the translation must match the original on the return value, the guarded trace, RBX and RSI over 545 input pairs per export. Fifteen patched copies must be rejected with their stated reason: swapped and missing `POP`s, a read of the caller's RBX, a removed store before a reload, a store removed on one path only, a `PUSH` on one path, a save-slot read, accesses to `[RSP]`, `[RSP+28h]` and `[RSP-8]` at entry, `LEA` and `MOV` copies of RSP, an unsaved write to RDI, and `MOV RSP, RAX` and `SUB RSP, RCX`. Rounding every frame offset down to 8 bytes fails `spill_reload`, and keeping a frame byte at a join when only one path wrote it accepts the one-path control. On Windows 11 build 26200 with MSVC 19.50 and ML64, all 2,725 calls matched and every control was rejected. The C++ generated for the existing jump-table, `addr32`, entry-order, narrow and fourth-argument fixtures is unchanged. Run `ctest --preset dev --no-tests=error -R '^cpu_aot_stack_frames$' -V`.

For a desktop corpus, a Release (`/O2`) build of this change on `9bdbb20` with MSVC 19.50 was walked over every `.pdata` entry of its 81 non-generated images, translating each unique function body with four arguments. Three CRT-free fixtures did not link at `/O2` and are not included. Of 1,406 unique functions, 44 translated before this change and 63 after it, and none was lost. Of the 19 new ones, 5 are this fixture, 9 are frames in other MASM fixtures and 5 are MSVC output in `pe_map.dll`, `era_query_check.exe` and `offscreen_check.exe`. Calls remain the first refusal of most of the corpus. Among the functions refused before the change, 37 now stop at an unsupported register: a RIP-relative operand in 28, an XMM or YMM register in 8 and AH in 1.

### Delay imports

`delay_route` imports `delayed_value` from `cpu_delay_provider.dll` by name and `delayed_ordinal` by ordinal 2 with `/DELAYLOAD`, and exports one caller for each. `cpu_native_delay` compares two paths: `LoadLibrary` plus the image's own delay helper, and `pe_map` with a resolver bound to the same provider. The mapped calls return the provider values, and IAT slot checks confirm both hold the `GetProcAddress` results before the calls. Binding the altered provider returns its values instead.

Windows leaves a delay IAT slot pointing at its thunk until the first call. `pe_map` resolves every delay descriptor at map time because it has no `LoadLibrary`, and the image's helper would bypass the host resolver. An absent provider therefore fails the map with `MAP_UNRESOLVED_IMPORT`, where Windows would map and fail on the first call. Descriptors must set `dlattrRva`; a descriptor without it stores virtual addresses and is rejected as unsupported.

Like the TLS and ordinary import tables, the delay-import directory is found by its address and `Size` is not used. The table is walked to the null descriptor; a copy with `Size` zero still binds, and a directory address of zero ignores a nonzero `Size`. Copies patched to clear `dlattrRva`, point the DLL name outside the image, zero the name table, point the IAT outside the image, or move the directory address outside the image are rejected before the provider is requested.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_delay$' -V
```

### Candidate programs for a named SGD5 effect

Supply the expected effect name, the group range, the program-array offset and the effect-record range. Use physical byte offsets in decimal or hexadecimal. Both ranges end at the first byte after the selected data.

```text
python -B tools/sgd5_effects.py archive.bin EFFECT --group GROUP_START GROUP_END --arrays ARRAY_OFFSET --record EFFECT_START EFFECT_END
ctest --preset dev --no-tests=error -R "^sgd5_effect_candidates$" -V
```

The report follows the effect's stored list order. Each entry gives its stored group, zero-based program index, name, numeric DXIL shader kind and decoded-container SHA-256. Check the DXIL kind when interpreting a group label. A stored VS group can contain a mesh shader (kind 13).

Two independently located local effects yielded 5 and 81 candidates across all groups. Their pixel lists contained 2 and 41 candidates respectively, and every pixel-container digest matched an independent decode. Identifying which candidate a draw uses still requires evidence of pass selection.

## Offline pixel shader recompilation

`tools/pixel_aot.py` extracts one named pixel program from a local archive, decodes gfx1013 instructions with LLVM and emits HLSL. DXC compiles the result to desktop DXIL before the draw. The lowerer tracks register writes across whole-quad interpolation of attribute 0, a scalar texture-descriptor load, a 2D RGBA sample, packed round-toward-zero binary16 conversions and one compressed color export. Unknown instructions, mismatched bindings and reads before the required wait cause translation to fail. [LLVM disassembler API](https://llvm.org/docs/doxygen/group__LLVMCDisassembler.html) · [Leonida public format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/fssm_unpack.py)

The caller identifies the entry registers for the texture address and interpolation base, and supplies the four sampler descriptor words to match. The generated pixel shader uses `TEXCOORD0`, texture `t0` and sampler `s0`. The test binds a point-clamp sampler, one mip and a full-screen triangle. These bindings are an explicit specialization. The test measures finite UNORM inputs under full pixel coverage. Execution with a title's original pipeline state, helper-lane behavior at triangle edges and other texture formats require separate validation.

For a shader that reads constants, pass `--constant-descriptor OFFSET` to identify its four-word descriptor in the same table as the texture descriptor. The desktop test binds 256 bytes at `b0`. A four-word read may span two HLSL constant vectors. Arithmetic support includes log2, exp2, reciprocal, min/max, addition, multiplication, subtraction in either operand order, legacy zero-preserving multiplication, multiply-add, destination accumulation and saturation. `v_mov_b32` copies a sample component, a loaded constant, an earlier result or an inline or literal constant, and rejects interpolation state, packed values and source modifiers. The lowerer rejects moves and floating-point arithmetic writes to the destination registers of an outstanding sample until `s_waitcnt vmcnt(0)`. As in the other operands, an integer inline constant is a bit pattern, not a converted value. Ordered and unordered `v_cmp_*_f32` comparisons, including the always-false and always-true predicates, but excluding class and exec-writing forms, become HLSL booleans, and `v_cndmask_b32` selects between two numeric operands with one. The lowerer accepts a mask in `vcc` or a scalar register under either wavefront-width spelling and rejects a select whose mask it did not see a comparison write. For `nlg`, equality or an explicit NaN check on either operand preserves the unordered case in DXIL. The earlier ordered comparisons produced a mismatch on the [reported RX 9070 XT driver](https://github.com/R0Y00/Xbox2Windows/issues/179). The pixel draw checks cover all 16 truth tables with less-than, signed-zero equality, greater-than and a quiet NaN on either side. Replacing a NaN with an equal finite operand must fail against the saved unordered reference. Compile arithmetic shaders with DXC `-Gis`. Transcendental precision and multiply-add rounding may vary by device. [AMD RDNA instruction reference](https://gpuopen.com/wp-content/uploads/2019/08/RDNA_Shader_ISA_5August2019.pdf) · [HLSL mad precision](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/mad)

`s_movk_i32` sign-extends its 16-bit immediate, and the translator folds the result. One draw checks the rule end to end on the GPU. It moves 0x8000 and 0x7fff, extracts bytes 1 and 3 of each with `s_bfe_u32`, and widens each byte as a binary16 subnormal scaled by 2^24 to the exact integer, so the draw exports 255, 128, 127 and 0. A reference for zero extension (byte 3 of 0x8000 as 0), and one for treating 0x7fff as negative (byte 3 as 255), must each report a mismatch.

Scalar ALU support also covers the 64-bit SOP1/SOP2 family (`s_cmov_b64`, `s_not_b64`, `s_brev_b64`, `s_and_b64`, `s_or_b64`, `s_xor_b64`, `s_andn2_b64`, `s_orn2_b64`, `s_lshl_b64`, `s_lshr_b64`, `s_ashr_i64`, `s_bfm_b64`) and the 32-bit scalar bitfield extracts (`s_bfe_u32`, `s_bfe_i32`), complementing the 32-bit scalar ALU, comparison and full SCC tracking scope of PR #257 without overlapping it. Each 64-bit scalar result becomes an HLSL `uint2` local bound across an even-aligned SGPR pair (`s0`..`s105`), and each 32-bit extract becomes a `uint` local; constant-buffer inputs are read with `asuint` and vector operands consume scalar results through `asfloat`. Inline decimal integers sign-extend to 64 bits, while 32-bit hex literals zero-extend for `b64` operations and sign-extend for `s_ashr_i64`. Operations that update SCC in RDNA hardware (`s_not_b64`, the 64-bit bitwise and shift forms, `s_bfe_u32` and `s_bfe_i32`) track a nonzero-result boolean so `s_cmov_b64` selects between the source and existing destination pair; `s_brev_b64`, `s_bfm_b64` and `s_cmov_b64` preserve SCC, and `s_wqm_b64` and the exec-restoring `s_and_b64` clear it. Pending scalar loads, unaligned or out-of-range pairs, descriptor/exec/vcc/VGPR operands, untracked SCC reads, exec save/restore ops (`s_*_saveexec_b64`) and out-of-scope scalar ops are rejected.

From the repository root on Linux with LLVM 15 installed, assemble the original test shaders, wrap them in synthetic records, then extract, decode and translate them. The test compares the result with lowering the source operands directly.

```sh
python3 -B tests/pixel_aot_check.py binary llvm-mc-15 llvm-objcopy-15 libLLVM-15.so.1
```

On Windows, use the Python-enabled build from the project README. Each backend draws a copy, a red/blue swap, a nonlinear curve and a compare-and-select program, the last two with two sets of constants each. It compares all four channels of 4,096 pixels bitwise with an independent CPU reference. Negative controls change the expected channel order, the bound gain or the comparison threshold and must report a mismatch. The select program uses only comparisons, one subtraction and selects, so its reference contains no transcendental or fused step and assumes only correctly rounded UNORM sampling. Its inputs are finite, so NaN operands remain unmeasured. The absolute-value fixture also draws abs(0.5 - red) and checks that removing the modifier produces a mismatch. The add, multiply and move program is drawn with two sets of constants, and replacing its reversed subtractions with forward ones must produce a mismatch. Its reference asserts that every addition, subtraction and multiplication is exact in binary32 for the test inputs, so it does not depend on rounding direction or fusion. The `scalar64` fixture exercises all 14 newly lowered scalar forms across two 12-word constant sets (including NaN bit patterns, negative 64-bit inputs, zero/small/large/wrapped shift counts, `s_bfm_b64` width boundaries, signed/unsigned `s_bfe_*` across `offset + width < 32` and `>= 32`, and SCC preservation through `s_cmov_b64`, `s_brev_b64` and `s_bfm_b64`), folding the 64-bit accumulator and 10 flipping SCC observation bits into four exact binary16 fields (`1 + k/1024`). Four mutations (`s_ashr_i64` to `s_lshr_b64`, `s_bfe_i32` to `s_bfe_u32`, `s_andn2_b64` to `s_and_b64`, and replacing an `s_cmov_b64` `-1` source with `0`) must each report `pixel_mismatch`. What this measures is the synthetic fixture translation, DXC compilation and bitwise D3D12 readback; exec save/restore control flow, SOPP branches, scalar-computed descriptors and real title pixel-shader execution remain unverified.

`v_cubeid_f32`, `v_cubesc_f32`, `v_cubetc_f32` and `v_cubema_f32` follow LLVM's `ConstantFoldAMDGCNCubeIntrinsic` (`llvm/lib/Analysis/ConstantFolding.cpp`). The major axis is z if |z| is at least |x| and |y|, otherwise y if |y| is at least |x|, otherwise x; a face is negative only for a nonzero, non-NaN negative coordinate, and `v_cubema_f32` returns twice the signed major coordinate. The lowering computes each result inline from the three operands. One draw evaluates all four operations for six directions that cover every face, the y/x and z/x ties and -0 on the major axis, which selects face 4 and exports -0 for the major axis. Each direction has a wrong reference for the property it isolates (the tie winner, the sign of -0, the sign of the major axis, the sign of a coordinate), and each must report a mismatch. NaN coordinates are not drawn.

The same binary and draw checks cover `v_trunc_f32` in e32 and e64 forms. The lowerer emits HLSL `trunc`, which rounds toward zero and returns a floating-point value. With two constant sets, the RTX 5080 and WARP draws matched all four channels of 4,096 pixels per draw against the CPU reference. The fixture covers both signs, signed zero, source negation and absolute value, and both clamp bounds. Replacing truncation with floor changed the first pixel on both adapters. Removing the upper clamp changed alpha from 1 to 2 in the first pixel on WARP. The source-built fixture verifies desktop readback with the bindings above. Execution with a title's own pipeline state remains unverified.

The `v_ceil_f32` fixture checks rounding toward positive infinity in e32 and e64 encodings. It compares finite integral and fractional inputs of both signs, including signed zero, and exercises e64 absolute value, negation and clamp. Replacing the e32 ceiling with a floor must produce a pixel mismatch. The translator rejects a ceiling write into a pending texture sample destination before `vmcnt(0)`. [AMD RDNA 2 ISA reference](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

`v_cvt_f32_f16` widens the binary16 in the low 16 bits of S0. The lowering builds the binary32 encoding from those bits, so a subnormal half stays exact as m × 2^-24 whatever the desktop denormal mode, and applies `abs` and `neg` modifiers after widening. The draw converts 1.0 with junk in the high half, -|-5.0|, the largest finite half 65504 and the smallest subnormal half 2^-24, which a multiply by the literal 2^24 turns into 1.0. References that read the high half, drop the negation, use a wrong exponent or expect the subnormal flushed must report a mismatch. NaN and infinite halves are not drawn; whether Scarlett quiets a signaling NaN when widening is not measured, and the lowering keeps the payload. SDWA selections of the high half are refused.

To supply a reference for another arithmetic program, run `offscreen_check.exe hardware --check-pixel-case shader.cso reference.case`. The case contains 256 bytes of constants followed by 64 expected RGBA colors in row-major little-endian binary32, one for each texel of the test texture. Generate the expected colors after round-toward-zero half export and widening back to float.

`v_fmaak_f32` computes S0 × S1 + K and `v_fmamk_f32` computes S0 × K + S1, where K is the 32-bit literal that LLVM prints in that position. Both use the `v_fma_f32` lowering; the translator refuses a register where the literal belongs. The draw computes each instruction twice with inputs 1.5, 2.5, -3.0 and 0.75 and the literals 0.25, 3.0, -0.5 and 0.5. Every product and sum is exact in binary32, so the reference does not depend on fusion. A reference that swaps the literal with S1 must report a mismatch in each channel.

`v_fract_f32` returns x - floor(x) clamped to `0x3f7fffff`, so its result stays below 1.0. The lowering uses an explicit clamp, not a bare D3D `frac`, which has no clamp and differs between adapters. For an input of -2^-30 the unclamped difference rounds to 1.0. An RTX 4070 Ti (driver 32.0.16.1088) exported 1.0 for that input, while WARP 10.0.26100.9818 already exported the clamped value. The draw also checks -0.25, 2.75 and -2^-23, which stay below the clamp. A reference that expects 1.0 for the first input must report a mismatch. NaN still propagates: it fails the clamp comparison.

`s_buffer_load_dword` reads one word from the tracked four-SGPR constant descriptor. Supported offsets are dword-aligned immediates within the 256-byte desktop binding, with `null` denoting offset zero. The lowerer requires the descriptor to be ready and tracks the destination until `s_waitcnt lgkmcnt(0)`. The synthetic binary and draw checks cover offsets 0, 8 and 252. Two sets of constant words matched all four channels of 4,096 pixels per draw on WARP and RTX 5080. Reading the neighboring word changes the first pixel on both adapters. [AMD RDNA 2 scalar memory rules](https://docs.amd.com/v/u/en-US/rdna2-shader-instruction-set-architecture)

```powershell
ctest --preset dev -R '^pixel_aot_' -V
```

For a compatible local record, substitute the archive, exact record name, two register numbers and four descriptor words below. Run the translator from the repository root in WSL, then compile and draw from a Windows development shell. The JSON printed by the translator identifies each input and output by SHA-256 for local reproduction. The renderer's `rgba` expectation checks a copy and `bgra` checks a red/blue swap.

```sh
python3 -B tools/pixel_aot.py "$archive" "$record" --llvm libLLVM-15.so.1 \
    --texture-sgpr "$texture_reg" --interp-sgpr "$interp_reg" \
    --sampler-words "$word0" "$word1" "$word2" "$word3" --output out/pixel-aot/translated.hlsl
```

When several records share the exact name, add `--container-sha256` with the SHA-256 of the full decoded DXBC container. Run the [shader extractor](#optional-compressed-record-inventory) with `--length-prefixed-names --format dxbc`, then hash the desired `.dxbc` file using `sha256sum` or `Get-FileHash -Algorithm SHA256`. Exactly one record must match both the name and the digest. Two containers can carry identical native instructions and different metadata, so their native program hashes can be identical.

```powershell
.\tools\dev.ps1
dxc -T ps_6_0 -E main -Fo out/pixel-aot/translated.cso out/pixel-aot/translated.hlsl
.\out\build\dev\offscreen_check.exe hardware --check-pixel out/pixel-aot/translated.cso rgba
.\out\build\dev\offscreen_check.exe warp --check-pixel out/pixel-aot/translated.cso rgba
```

## Package integrity

For a local build with a multi-file v1 torrent and `layout*.xml`, run the offline verification with Python 3.9 or newer. Substitute your local directory name.

```powershell
python tools/verify_build.py "game-files/<build-directory>"
```

The script verifies SHA-1 pieces across file boundaries in torrent order and compares layout destinations separately with disk and torrent entries. The report is `out/integrity/<build-directory>.json`. Exit code 0 means the payload passed piece verification. `layout.absent_from_disk` lists missing layout entries separately.

## PE dependency baseline

The scanner uses x64 Windows, Python 3.11 or newer and pefile. Set up an isolated environment once.

```powershell
python -m venv out/pe-env
.\out\pe-env\Scripts\python.exe -m pip install pefile==2024.8.26
```

Capture a DLL-only baseline. When an EXE becomes available, write a separate report and compare it with the saved baseline.

```powershell
$pePython = '.\out\pe-env\Scripts\python.exe'
$buildDir = 'game-files/<build-directory>'
& $pePython tools/pe_inventory.py $buildDir --output out/dependencies/pre-exe.json
# Run the next command after adding the main executable.
& $pePython tools/pe_inventory.py $buildDir --baseline out/dependencies/pre-exe.json --output out/dependencies/with-exe.json
```

`summary` gives counts. `unresolved_edges` lists requested exports missing from both the package and the host. Each `imports` entry identifies the caller, import mechanism, provider and symbol. `#N` denotes an ordinal. The scanner reads import descriptors and lookup tables itself. pefile skips names outside letters, digits and `._?@$()<>` and drops a descriptor left empty, so those entries would otherwise vanish from the graph. Ordinal imports stay `#N` even where pefile's built-in table supplies a name. Each provider is listed under the file name the loader looks for: a descriptor name without an extension gets `.dll` appended, while a trailing dot suppresses the append and is dropped, as with `LoadLibrary`. Without this, an import of `intake_forward` would miss the package's `intake_forward.dll` and report `provider_not_found`; `bundled_export_forwarders` rewrites the caller's descriptor to both forms. [LoadLibraryW extension rule](https://learn.microsoft.com/en-us/windows/win32/api/libloaderapi/nf-libloaderapi-loadlibraryw) · [Wine DLL name lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c) `images` includes architecture, SHA-256, export names, ordinals and forwarders. `diff` distinguishes new caller relationships from dependencies seen for the first time in the build.

The scanner reads System32 export tables and applies the host API-set v6 map at each hop, using the forwarding DLL as the importer at the next hop. Each import's `desktop_resolution.chain` records the requested module, selected provider, symbol and any advertised forwarder. Its final status identifies a terminal export, missing provider or export, malformed forwarder, or cycle. `desktop_export` retains the first-hop result for older report consumers. The comparison covers System32 DLLs on the measured machine. Package selection at runtime, parameter layouts and behavior require separate evidence. [PE export address table](https://learn.microsoft.com/en-us/windows/win32/debug/pe-format#export-address-table) · [Wine API-set lookup](https://github.com/wine-mirror/wine/blob/master/dlls/ntdll/loader.c)

Package DLLs can also forward exports, to another package DLL, to themselves, by ordinal or to System32. An edge whose provider is in the package gets a `package_resolution` chain. It uses the same statuses, adds `ambiguous_package_basename`, and marks each hop's `package` origin. Plain module names select a uniquely named package image before System32. API-set contracts always select System32 hosts. KnownDLLs, which take precedence over the application directory, are not modeled. An edge counts as resolved through the package only if its chain reaches a terminal export. `dependency_paths` adds each packaged forwarder target after its provider with the reason `forwarder` and reports a broken chain under the chain's terminal status. Earlier tools treated any matching export name as resolved. They omitted the target module and accepted forwarders to missing exports, missing modules or cycles. The `bundled_export_forwarders` test builds `intake_forward.dll` with six placeholder exports, because linkers differ in how a .def file declares forwarders. It moves the export table of a temporary copy into a new section and turns the six exports into forwarders: by name, by ordinal, a two-hop chain through itself, a missing target export, a self-cycle and an absent module. It then checks the inventory and path report for a caller that imports all six.

On Windows build 26200, the pre-executable inventory covered 16 package DLLs and 1,337 import edges. Of these, 171 edges (78 distinct requested module/symbol pairs) went through a single export forwarder. All 171 reached a terminal export. In total 1,329 edges resolved on the desktop. The eight remaining edges requested six distinct interfaces across `xmem.dll` and `d3d12_xs.dll`. The 16 package DLLs had zero forwarded exports of their own. The historical baseline was preserved for comparison.

## Heap and process memory

`src/era_heap.cpp` is a desktop handler. It splits the attribute word using SlimEra's Xbox One ERA layout: object type in bits 0–13, page size in bits 14–15, allocator ID in bits 16–23, log2 alignment in bits 24–28, memory type in bits 29–32 and reserved bits 33–63. Object type and allocator ID are carried as tags and do not affect placement, as in SlimEra's default heap.

The desktop handling is:

- **Heap (memory type 0):** served from the CRT heap at the requested alignment, minimum 16 bytes, and zero-filled. The page size does not affect these requests. SlimEra passes every heap request to `_aligned_malloc`, which commits the alignment padding as heap memory, so a 257-byte request with exponent 31 would commit more than 2 GiB. The desktop handler instead gives alignments above the page size their own reservation and commits only the pages that hold the payload; `VirtualAlloc` returns them zeroed.
- **Graphics (memory types 1–6):** committed as a separate `VirtualAlloc` region. `release_with_attributes` frees it with `VirtualFree`. The region starts on the desktop allocation granularity, which covers 4 KB and 64 KB pages. This is ordinary CPU-visible memory; no GPU mapping or caching behavior is modeled.
- **Rejected:** physical memory types 7–9, undefined types 10–15, the undefined page-size value 3, reserved bits, and graphics requests whose page size or alignment exceeds the allocation granularity all return a null pointer.

`release` frees heap allocations. It frees a page-backed block with `VirtualFree` when `VirtualQuery` reports it as the base of its own allocation; a CRT block never is, because `_aligned_malloc` stores its base pointer below the returned address. `release_with_attributes` takes the allocation's attribute word, as `XMemFree` callers do.

What is measured is the desktop behavior above. Under the ERA layout, the two recorded words give alignments that match their callers' own arithmetic: 32 bytes for Bink and 256 bytes for AMD. The following remain unconfirmed on Scarlett:

- that Scarlett uses the ERA layout at all;
- what allocator ID `0xe5` means;
- the GPU visibility and caching of memory type 5 and the other graphics types;
- the console's error conventions.

[SlimEra attributes](https://github.com/XWine1/SlimEra/blob/main/era.h#L105-L137) · [SlimEra default heap](https://github.com/XWine1/SlimEra/blob/main/kernelx.cpp#L600-L657) · [Issue #126](https://github.com/R0Y00/Xbox2Windows/issues/126)

| CTest case | Inputs and checks |
|---|---|
| `era_heap_allocation` | Allocate 257 bytes using six alignment exponents. Check alignment of at least 16 bytes and the requested alignment, initial zeroing, writes and release. Debug builds also compare CRT heap snapshots. |
| `era_heap_failure` | Each reserved bit, combined with a heap alignment and with the AMD graphics word, returns a null pointer. So do memory types 7–15, page-size value 3, graphics memory with 4 MB pages or a 128 KiB alignment, zero size and excessive size. |
| `era_heap_reuse` | For three alignment exponents and three sizes, fill a block with `0xa5` and release it. Then hold up to 32 new blocks of the same shape until the released address returns. Every new block must read as zero. The output reports `recycled_address`, the number of the nine cases in which the CRT returned the dirty address. glibc returned all nine in a Linux build with a `posix_memalign` stand-in. Windows 11 build 26200.9974 with MSVC 14.44.35207 returned the dirty address in all nine cases in both Debug and Release. Low-fragmentation heap randomization can lower it. |
| `era_heap_observed_words` | Desktop handling of the two recorded words. Under the ERA layout, the Bink word `0x05e50000` splits into heap memory, 4 KB pages, allocator field `0xe5` and 32-byte alignment. The AMD word `0xa8004000` splits into memory type 5, 64 KB pages, allocator field 0 and 256-byte alignment. Bink-style 97- and 193-byte blocks are 32-byte aligned and zeroed, and each wrapper recovers its base. AMD-rounded requests of 256 to 65,792 bytes start a committed read-write region on a 64 KiB boundary and are zeroed and writable. After release, `VirtualQuery` reports the region as free. Nonzero object-type and allocator tags leave heap placement unchanged. |
| `era_heap_large_alignment` | Exponents 13, 16, 17, 20 and 31 with sizes of 1 and 257 bytes, one page, one page plus one byte and three pages minus one byte. Each block must be aligned and zeroed. `VirtualQuery` must report the block as the base of its own private read/write allocation, with a committed region equal to the payload rounded up to whole pages. After release the address must be `MEM_FREE`. The output reports the 25 cases, the committed bytes and the summed alignments. With the previous `_aligned_malloc` path, the first case already fails because the block lies inside a CRT heap allocation. |
| `era_heap_concurrency` | Four threads run 100 rounds each. They hold simultaneous allocations, write distinct values, compare every byte and release the memory. |
| `era_heap_caller_layout` | Owned desktop storage covers all 32 base residues at eight payload sizes. Each Bink-style wrapper recovers its original base after a payload write and preserves the surrounding guard bytes. Ten AMD-style requests check 256-byte size rounding and the unused tail. These cases establish caller-side arithmetic. The Bink-style storage uses the recorded Bink word. |

`desktop_memory_query` supplies Windows observations for `XMemVirtualQuery` and `XMemGetWorkingSetStatistics`. The test reserves memory, commits and writes 1,024 pages, checks the read-only/writable boundary, then decommits. It verifies residency of a locked page and records working set, peak working set and private commit at three points. Values vary between runs. Scarlett structure layout, units and accounting scope remain unconfirmed. [Windows working sets](https://learn.microsoft.com/en-us/windows/win32/memory/working-set)

The same test creates writable and copy-on-write views of a two-page, pagefile-backed mapping. After a write through the copy view, `VirtualQueryEx` reports `MEM_MAPPED` for both pages. With both pages locked and resident, `QueryWorkingSetEx` clears `Shared` only for the written copy page. Writes through the other view remain visible on the untouched page. [Windows copy-on-write queries](https://learn.microsoft.com/en-us/windows/win32/api/memoryapi/nf-memoryapi-virtualqueryex#remarks)

`src/era_query.cpp` is a desktop handler for `XMemVirtualQuery`, shaped by the four BVH call sites. It writes the Win32 `MEMORY_BASIC_INFORMATION` to bytes `0x00`–`0x2F`; the callers read State at `+0x20` and Protect at `+0x24`, matching that layout. The byte at `+0x30` has no public definition: the Microsoft reference for `XMemVirtualQuery` and its result structure is restricted to the Xbox developer program.

The handler therefore fills that byte by a desktop policy, not by a confirmed Scarlett field:

- it writes `0x01` when the queried address lies in a region registered with `register_gpu_region`, and `0x00` otherwise;
- later bytes are left untouched;
- the policy applies only to queries of the current process;
- a buffer shorter than 48 bytes fails with `ERROR_BAD_LENGTH`, as `VirtualQueryEx` does.

With registration, the callers' predicates accept GPU-intended memory and reject everything else. What the console's tail byte means, and what sets it, remain open. The selector `0x20000001` and the output units of `XMemGetWorkingSetStatistics` have no public reference either; the desktop `xmem.dll` below reports that call as unsupported.

| CTest case | Inputs and checks |
|---|---|
| `era_query_fields` | Reserved, committed read/write, read-only and no-access pages, each queried with and without registration. Bytes `0x00`–`0x2F` match a plain `VirtualQueryEx` result byte for byte, and bytes after `+0x30` keep their sentinel. |
| `era_query_tail` | Negative control: before registration a committed read/write page fails the tail check. After registering two pages, the tail byte is set at the first, middle and last byte and clear on the next committed page and on a reserved page. The callers' predicates accept read/write and read-only, reject the source check on no-access, and reject both after decommit. A real handle to the process counts as the current process, and unregistering clears the flag. |
| `era_query_registry` | Null, empty and wrapping ranges are refused. Exact, front, back, containing and contained overlaps are refused, adjacent ranges are accepted, and unregistering needs the exact base. |
| `era_query_short` | A 47-byte buffer returns 0 with `ERROR_BAD_LENGTH` and is left untouched. A 48-byte buffer succeeds without writing the tail byte. A null output returns `ERROR_BAD_LENGTH` at 47 bytes and `ERROR_NOACCESS` at 48 or 64 bytes, matching desktop `VirtualQueryEx`. |

### Desktop xmem.dll

`src/xmem_shim.cpp` builds `xmem.dll`, which exports the four functions the package DLLs import by name. The call shapes come from the observed call sites, not from a published header:

| Export | Observed call shape | Desktop handling |
|---|---|---|
| `XMemAlloc` | Size and attribute word (Bink and AMD callers). | `era_heap::allocate`. Graphics memory types are also registered with `era_query`, so they count as GPU-intended. |
| `XMemFree` | Allocation and the same attribute word. | Unregisters graphics allocations, then `era_heap::release_with_attributes`. |
| `XMemVirtualQuery` | The four BVH callers pass the process in `rcx`, the address in `rdx` and a 64-byte buffer in `r8`; `r9` is not set, so there is no length argument. They test the 32-bit result in `eax`. | `era_query::query` on the 64-byte buffer; returns `TRUE` when it succeeds. |
| `XMemGetWorkingSetStatistics` | The seven AMD callers pass `0x20000001` in `ecx` and an output address in `rdx` and test the 32-bit result. On failure each uses zero instead of the 64-bit value at `+8`. | Returns `FALSE` with `ERROR_NOT_SUPPORTED` and leaves the output untouched. |

The registration of graphics memory and the unsupported working-set call are desktop policies, not confirmed Scarlett behavior.

A local scan of the inspected package's 16 DLLs together with this `xmem.dll` (`tools/pe_inventory.py`) resolved all six xmem import edges to it. The unresolved edges dropped from eight to two, both `d3d12_xs.dll` root-signature functions. The scan reads the images and runs no package code.

| CTest case | Inputs and checks |
|---|---|
| `xmem_shim_exports` | The four names resolve through `GetProcAddress`, and the test's named imports bind to the same module. |
| `xmem_shim_allocation` | The Bink word returns 32-byte-aligned, zeroed heap memory. The AMD word returns committed read/write memory on a 64 KiB boundary that `XMemFree` releases (`MEM_FREE`). Zero size, a reserved bit and a physical memory type return null, and freeing null is a no-op for both memory types. |
| `xmem_shim_query` | Graphics memory from `XMemAlloc` passes both BVH predicates and keeps the sentinel after `+0x30`, through the pseudo-handle and a real handle. After `XMemFree` the address reads as `MEM_FREE` with a clear tail. Heap memory fails the tail check, and an address outside user space returns `FALSE` without touching the buffer. |
| `xmem_shim_working_set` | Returns `FALSE` with `ERROR_NOT_SUPPORTED` and leaves the output untouched, so the callers' value is zero. |

### Desktop GPU addresses for title allocations

On the console a graphics allocation's CPU pointer is also its GPU virtual address. Titles pass those addresses to D3D12 calls, and the BVH builder's shaders read 64-bit addresses from their input records. A desktop D3D12 resource gets a GPU virtual address chosen by the driver.

- `era_gpu_alias::open` (`src/era_gpu_alias.cpp`) opens an existing `VirtualAlloc` allocation, such as an `era_heap` graphics allocation, with `ID3D12Device3::OpenExistingHeapFromAddress`. It then places one cross-adapter buffer with unordered access over it.
- The title pointer and the buffer's GPU virtual address then name the same system-memory bytes, without copies.
- `era_title_va::Table` (`src/era_title_va.cpp`) maps title address ranges to those desktop GPU virtual addresses.

What is covered:
- Translating title addresses that D3D12 calls receive. Root views at translated addresses are exercised below.
- Shader loads through 64-bit title addresses read from memory, via `src/title_va_lookup.hlsli`.
- Stores, integer add and sub atomics, and float min and max (a compare-exchange loop) through those addresses, on the same aliased system-memory heaps.

Not covered:
- Performance. GPU access to this heap goes over the bus.

How the shader lookup works:
- `era_title_va::shader_ranges` packs the table's sorted ranges: low and high words of begin and end, and a descriptor slot.
- `TitleLookup` binary-searches them with 64-bit comparisons on `uint2`. It takes the access size and succeeds only for a 4-byte-aligned address whose whole access lies inside one range. It then returns the slot of the buffer that aliases the range, and the byte offset, which is exact because a range is shorter than 4 GiB. `shader_ranges` also requires each range to start on a 4-byte boundary.
- The shader then loads through that slot's raw UAV.

| CTest case | Inputs and checks |
|---|---|
| `era_title_va_translate` | Offsets inside two ranges, the last word of a range, and a zero byte count translate to the matching desktop address. |
| `era_title_va_bounds` | Bytes just before and after a range, spans crossing its end, a span longer than the range and a wrapping span return 0. |
| `era_title_va_registry` | Empty, wrapping, zero-GPU and overlapping ranges are rejected; adjacent ranges are accepted and listed in order; a removed range no longer translates. |
| `gpu_alias_warp`, `gpu_alias_hardware` | A 256 KiB memory-type-1 allocation from `era_heap` is opened as a heap. A compute shader reads 4,096 words through a root UAV at the translated title address `+0x1000` and writes transformed words through a root UAV at `+0x20000`. The CPU checks every result and the unchanged source through the title pointer, with no readback buffer. 64 guard words after the written range must keep their sentinel. Opening a pointer `0x1000` into the allocation must return `E_INVALIDARG` from `era_gpu_alias::open` without reaching D3D12; passing it to `OpenExistingHeapFromAddress` makes the debug layer report message 1088. Returns 77 when the adapter does not report `D3D12_FEATURE_EXISTING_HEAPS`. |
| `era_title_va_shader` | Packing splits begin and end into low and high words, including a range across a 4 GiB line; slots follow table order; unsorted, overlapping, empty, 4 GiB and unaligned-start ranges are rejected, and a length that is not a multiple of 4 is accepted. |
| `gpu_alias_lookup_warp`, `gpu_alias_lookup_hardware` | Three memory-type-1 allocations and one placed across a 4 GiB line are opened as heaps, packed, and bound as raw UAVs in a descriptor table. The placed range ends two bytes short of its allocation, so its last aligned word crosses the range end. A compute shader loads one DWORD through each of 150 title addresses read from an input buffer: starts, middles, each range's last valid word and the aligned word after it, misaligned starts at both ends, the words just outside each allocation, the null page, an address above user space, and the same low words 4 GiB above and below the placed allocation. Values and slots must match the CPU reading through the title pointers; misaligned, tail-crossing and out-of-range addresses must return the miss marker. The hit and miss split varies with where the allocations land. |
| `gpu_alias_lookup_writes_warp`, `gpu_alias_lookup_writes_hardware` | The same four allocations. A compute shader runs 1,047 records through the lookup: 14 stores (range starts, middles, each last valid word, and the words on either side of the 4 GiB line, all distinct); 256 contending threads each for add, sub, float min and float max on one word per operation; and nine rejected records (stores to unmapped addresses, a store and a float min on the tail-crossing word, misaligned store, add and sub inside ranges), which must report 0. Every word of all four allocations is then compared with the CPU expectation through the title pointers, so rejected writes, including the in-range bytes of the tail-crossing word, must leave the backing words unchanged. A compare-exchange loop that does not retry after losing a race passes on WARP, which showed no conflicting update. It fails on hardware. Before the fixture, a probe on four plain zeroed buffers in the same kind of UAV table has 256 threads each add 1 through a per-thread array index; each buffer must read 64, and the expected and actual counts are always printed. The fixture then runs in full and reports two groups, each with a mismatch count and its first mismatch: the shared add and sub words, and everything else (the per-record results and every other word). Wrong probe counts or any mismatch fail the test. It returns 77 only when the adapter does not support existing heaps. |

Measured at commit `55cea41` on Windows 11 build 26300, with the debug layer unavailable (Graphics Tools not installed):
- `gpu_alias_hardware` passed on an NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088).
- `gpu_alias_warp` passed on WARP 10.0.26100.9818.
- On both, the interior open returned `E_INVALIDARG` (`0x80070057`).
- At commit `985f0ab`, `gpu_alias_lookup_hardware` and `gpu_alias_lookup_warp` passed on the same two adapters with 4 ranges and 122 records, mismatches 0 (one run: 104 hits and 18 misses).
- At commit `8bf7666`, `gpu_alias_lookup_writes_warp` passed on WARP 10.0.26100.9818: 1,041 records, sum 128416, min -1250, max 1241.62, three misses, 147,456 words checked, mismatches 0. At the next commit, `gpu_alias_lookup_writes_hardware` passed on the NVIDIA GeForce RTX 3070 Laptop GPU (driver 32.0.16.1088) with the same values, 10 of 10 repeated runs. A compare-exchange loop without retry is caught there (the max word came out low).
- At commit `1c19cb6` (whole-DWORD validation), both lookup tests passed on WARP 10.0.26100.9818: lookup 150 records with 48 misses; writes 1,047 records with 9 rejected, 147,456 words checked; mismatches 0. Removing the span check, the alignment check, or both (the earlier first-byte check) fails both tests. The hardware variants were not re-run for this commit (the GPU was in use).
- On Windows 10 22H2 (build 19045) the in-box WARP 10.0.19041.5794 gives a wrong result, so `gpu_alias_lookup_writes_warp` fails there: in the atomics probe each buffer reads 256, not 64. In the fixture this made the add word `0x7d680` (four times the sum `0x1f5a0`) and the sub word `0x76bc0` (1,000,000 minus four times the sum); stores, compare-exchange and the per-record results were correct. The factor is 4 for arrays of 2, 3, 8 and 16 UAVs, for raw, structured and typed views, with or without `NonUniformResourceIndex`, and `InterlockedXor` is also applied an even number of times. A single UAV and an index read from a constant buffer are exact; a literal index of 2 updates element 0. DXC 1.7.2308, 1.8.2502 and 1.9.2609 give the same result there. App-local WARP 1.0.9 through 1.0.22 and an NVIDIA GeForce RTX 3060 (driver 32.0.15.9636) are exact for the array of 8 and for the literal index, and pass the fixture. On the failing WARP the fixture reports the add and sub words as the only two mismatches: the other group, 1,047 per-record results and 147,454 words, has none. At commit `246990b` the Windows 11 in-box WARP 10.0.26100.9818 read 64 in all four probe buffers with mismatches 0 (measured by the fixture's author).

## Offscreen rendering and readback

`offscreen_hardware` selects a high-performance adapter. `offscreen_warp` selects Microsoft's software rasterizer. Both draw the same DXC-compiled triangle to a 64×64 RGBA8 texture. After GPU completion the test reads the texture using its row pitch and compares colors and coverage pixel by pixel. Outputs are `triangle_hardware.bmp` and `triangle_warp.bmp` in the build directory. `ctest --preset dev -V` reports the adapter, driver, debug layer and pixel differences. Error or corruption messages from an available debug layer fail the test.

The original test machine had Windows Graphics Tools installed. A second build in `out/build/agility` used Microsoft Agility SDK 1.620.2. Set `-DOFFSCREEN_AGILITY_620_DIR=<directory containing the x64 SDK DLLs>` to reproduce it. This configuration verifies debug interface activation. [Agility SDK package](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2)

### Windows 10 with the app-local Agility SDK and WARP

The Windows 10 22H2 in-box runtime caps an RTX 3060 at SM 6.5 and the in-box WARP (10.0.19041) at SM 6.2 with no DXR, so the `cs_6_5` BVH fixtures, the DXR checks and forced wave64 skip there. Without Graphics Tools, the debug layer reports `DXGI_ERROR_SDK_COMPONENT_MISSING` even when the SDK layers are app-local. The Agility build therefore continues with `debug_layer=unavailable` for that one result, and other debug-layer failures still stop the test. Capabilities reported on Windows 10 19045.6466:

| Runtime | RTX 3060 (32.0.15.9636) | WARP |
|---|---|---|
| In-box | SM 6.5, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 | SM 6.8, DXR tier 1.2, 32 lanes | 10.0.19041: SM 6.2, no DXR, 4 lanes |
| Agility SDK 1.620.2 and WARP 1.0.22 | SM 6.8, DXR tier 1.2, 32 lanes | 1.0.22: SM 6.8, DXR tier 1.2, 4 to 128 lanes |

Extract the [Agility SDK](https://www.nuget.org/packages/Microsoft.Direct3D.D3D12/1.620.2) and [WARP](https://www.nuget.org/packages/Microsoft.Direct3D.WARP/1.0.22) packages, then configure with both directories. The Agility directory holds `D3D12Core.dll`, `d3d12SDKLayers.dll` and the package renamed to `sdk.nupkg`. The WARP directory is the package's `build/native/bin/x64`:

```powershell
cmake --preset dev -B out/build/w10 -DOFFSCREEN_AGILITY_620_DIR=<agility dir> -DOFFSCREEN_WARP_DIR=<warp dir>
cmake --build out/build/w10
ctest --test-dir out/build/w10 --no-tests=error
```

`OFFSCREEN_WARP_DIR` requires `OFFSCREEN_AGILITY_620_DIR`. On the in-box runtime, WARP 1.0.22 reports DXR tier 1.2, but `dxr_query_warp`, `dxr_update_warp` and `bvh_differential_warp` fail with every ray reported as a miss. With the Agility runtime the same WARP passes them. Configuring the same build directory again without `OFFSCREEN_WARP_DIR` deletes the copied `d3d10warp.dll`, so `offscreen_check` goes back to the in-box WARP. `app_local_warp_toggle` checks this with a stand-in DLL.

In this configuration all `bvh_*_warp`, `dxr_*_warp` and `bvh_differential_warp` tests pass, and the hardware wave64 check skips at its 32-lane range with SM 6.6 available. `bvh_wave64_warp` now runs and fails at field 25, `WaveReadLaneAt(v, (lane*7+3) % 64)`. In two groups, all 128 lanes received the value of lane m where `7m+3 ≡ lane (mod 64)`, which is the inverse of the requested shuffle. Field 24's shuffle, the mirror read and the native-width run pass. At WARP's native width of 8, `7n+3 ≡ 3-n (mod 8)` is self-inverse, so the default `bvh_waves_warp` cannot show the difference. `wave_shuffle_warp` narrows the cause. WARP 1.0.22 runs a forced wave as blocks of its native 8 lanes, and it picks the source block using the request of the wrong lane: the lane of the reader's block that sits at the requested position. For this shuffle that equals the inverse permutation. A rotation by one lane gets 16 of 64 reads wrong at widths 16, 32 and 64, and none of them match its inverse. Reads where every lane of a block requests the same source block are unaffected. [Open question](https://github.com/R0Y00/Xbox2Windows/issues/145)

## Mapped DLL draw with a Windows reference

`cpu_native_frame_hardware` and `cpu_native_frame_warp` load the same source-built DLL with `LoadLibraryW` and with `pe_map`. Both paths record TLS before the DLL entry on process attach and detach. The DLL's `guest_draw` export selects the shared two-texture shader fixture and calls the graphics provider through its bound import. The provider calls the host test, which runs the existing offscreen renderer in a child process. The renderer checks every RGBA channel against the CPU reference, and the test compares the two 64×64 captures byte for byte. Withholding the graphics provider must fail mapping before startup or drawing.

```powershell
ctest --preset dev --no-tests=error -R '^cpu_native_frame_(hardware|warp)$' -V
```

On Windows 11 build 26200, the RTX 3060 Ti (driver 621.14) and WARP each matched all 4,096 pixels per load path with zero D3D12 debug warnings or errors.

## Static executable intake

By default, the intake tool locates the Scarlett entry in the sole build's `MicrosoftGame.Config`. It accepts the layout path or a separate candidate file and opens the game files for reading only.

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py
# Inspect a separately supplied candidate.
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py "E:\incoming\game_scarlett_master.exe"
```

Each run writes a new JSON report in `out/intake/`. Exit codes are 3 for a missing layout executable, 2 for an input or inspection failure and 0 for completed inspection. The report links the previous verification time, torrent SHA-256, install chunks and baseline summary. The inspected torrent omitted the EXE. The 13,345 matching pieces and 223,880,932,870 bytes verified in the historical report cover that original payload. A later EXE needs separate provenance. Intake rescans the package PEs and lists changed DLLs.

For each candidate the tool records SHA-256, architecture, entry RVA and section, TLS directory and callback order, then compares imports, modules and unresolved items with the baseline. Source-built PE fixtures and a miniature torrent cover named, ordinal and delayed imports, import names that pefile rejects, missing providers, TLS callbacks, invalid callback addresses, truncation, an absent entry and path conflicts. These checks establish static properties. Authenticity and startup remain unverified.

`pe.startup.locations` lists TLS callbacks in table order followed by the executable entry. Each location includes its RVA, section and file offset. When the x64 exception directory supplies a covering function interval, the report includes its inclusive start, exclusive end and unwind data RVA. A location can legitimately lack a covering record. An absent or unusable table leaves the independently observed startup locations available. In the local debug fixture the table had 110 records, one covering its TLS callback. The entry had no covering record. CTest cross-checks against pefile and exercises both absent and incomplete directories. [Microsoft x64 unwind data](https://learn.microsoft.com/en-us/cpp/build/exception-handling-x64)

### Frame plane requests in BinkAllocateFrameBuffers

One iteration of the exported frame-buffer allocator issues up to five requests to the [group descriptor append helper](#grouped-allocation-descriptor-builders). Slots 0, 3 and 4 use the first pitch and height. Slots 1 and 2 use the second pair. With pitches 96 and 32 and heights 7 and 3, enabling all five produced sizes 672, 96, 96, 672 and 672 in that order. Each call received the address of the selected plane's destination field, and its pitch field was written after the call.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_frame_planes$'`. Setting `BINK_DESCRIPTOR_IMAGE` also enables the `cpu_bink_package_frame_planes` test after building `cpu_bink_package_descriptors`. That comparison translates the loop body from RVA `0x2257` up to `0x2325` in the fingerprinted image. It supplies a frame index, the matching cursor and two preselected pitches. A desktop observer records the arguments at the five append sites. Pitch selection, the outer loop and backing storage allocation are outside this check.

### Frame buffers through the shared Windows heap

The integrated package replay enters the allocator with a context, frame table and requested alignment. It translates the function body, including format-based pitch selection, frame count adjustment, the five plane sites, the group allocation and the owner and accounting writes. The Windows compiler provides the stack prologue and epilogue. The group and heap functions are the same translations used in their individual tests. The resulting DLL imports `XMemAlloc` and `XMemFree` by name from the shared desktop `xmem.dll`.

The public control combines a source-written outer function with the translated MASM plane loop, descriptor helpers and grouped heap caller. Both builds run 36 cases, including all plane-enable masks on one frame, empty requests, two-frame allocation and the three-to-two count adjustment. Each enabled plane is filled independently and the entire backing block is compared, including padding. A second link of the same objects records the import arguments and forwards to the shared DLL, or injects one null allocation. On failure the caller returned zero, kept the owner null and destination slots unchanged for requested planes, and retained the pitch and accounting updates. Retrying the same context succeeded. The normal release returned the original heap base once with the allocation attributes.

Build the dev preset and run `ctest --preset dev --no-tests=error -R 'frame_to_heap$' -V`. For the local package case, configure the same fingerprinted DLL for `BINK_DESCRIPTOR_IMAGE` and `BINK_HEAP_IMAGE` and the grouped and heap entries documented below. The scenarios supply context and frame metadata; video stream decoding is outside this test.

### Grouped allocation descriptor builders

In the inspected Bink image, the function interval beginning at RVA `0x67c0` contains direct calls to the group initializer (`0x25b80`), append helper (`0x25b20`) and grouped allocator (`0x25a40`). The initializer places two adjacent arrays after a 64-byte header. Each successful append records a destination and a padded stride. With five available slots, requests of 0, 33, 1, 1025 and 32 bytes produced strides of 32, 64, 128, 1280 and 512 bytes in order. The stride in 32-byte units is congruent to the previous total in units plus one, modulo 32. All five stored strides were even. These helpers do not establish who produces odd tagged entries.

Build `cpu_bink_descriptors_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_bink_descriptors$'`. The test compares a source-written MASM implementation and its translation against explicit expected arrays after every append. Set `BINK_DESCRIPTOR_IMAGE` to the local Bink DLL to enable the fingerprint-checked `cpu_bink_package_descriptors` target and test. In the translated interface a capacity breakpoint returns status zero, allowing the test to check that both arrays and their guard word survive an extra append. The native control runs only within capacity.

### Bink allocation and release on the Windows heap

The exported binding now serves requests from `era_heap`, so the Bink shim and the heap experiments share one attribute decoder, alignment rule and release path. `bink_heap_check.py` checks on the binding directly that a 97- or 129-byte request with `0x05e50000` returns a 32-byte-aligned, zeroed block and that the matching release frees it.

The local test lowers the allocation and release callers and the exported `BinkSetMemory` setter from x64 machine code to C++, links the result to a Windows process-heap adapter, and runs the new DLL. The translator follows the setter's export jump and binds its two pointer stores to the same slots the allocation path reads. It also resolves the named PE imports, including tail transfers through linker thunks. The console-specific meaning of the heap attributes remains open.

On Windows, the public native fixture, its translation and the locally translated callers passed the same checks:

- Lengths 0 and `0xffffffff` returned null before reaching the allocator. Three forced allocation failures returned null after one request each. The largest input, `0x100000001`, verified 64-bit size handling without a large allocation.
- Each accepted length requested 64 extra bytes with attributes `0x05e50000`. Returning heap-backed storage at each of the 32 possible base residues produced a 32-byte-aligned user pointer. The check wrote the entire payload and compared both header bytes and the surrounding guards.
- With 32 allocations live, releasing them in reverse order passed each original base to the adapter once with the allocation attributes. All 32 backing blocks were freed by `HeapFree`. Releasing null made no allocator call.
- Callback pairs A and B each received one allocation request (97 and 193 bytes for 33- and 129-byte payloads). Both blocks contained the corresponding release function address, and the payload and guards matched. After installing B, releasing A's block called A's release function once with A's original base. After clearing the callbacks, releasing B's block likewise called B once with B's base. Neither operation called the XMem adapter.
- A null custom return for a 65-byte payload led to exactly one 129-byte `XMemAlloc` request. After switching callbacks, that block went back to `XMemFree` once with the original base and attributes. When both allocation attempts failed the caller returned null. A separate all-ones custom return stopped the request before the XMem adapter.

For the public control, build `cpu_bink_heap_lifted` in the dev preset, then run `ctest --preset dev -R '^cpu_aot_bink_heap$' --no-tests=error -V`. For a local package, set the CMake cache entries `BINK_HEAP_IMAGE` to the DLL path and `BINK_HEAP_ENTRIES` to the semicolon-separated allocation and release function **entry** RVAs for that image. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_heap`. Generated translations stay in the build directory.

The grouped caller at RVA `0x25a40` in the inspected Bink DLL tail-calls the allocator at `0x25bf0` when its first argument is null. A 33-byte prefix becomes 64 bytes before the allocator adds its own 64 bytes. With a group and 48 extra bytes, the same input produces a 176-byte request at the heap provider. On success, an odd length word selects a pointer slot relative to the returned block and an even word selects an external slot. Clearing the low bit gives the payload stride. On allocation failure both the extra-byte total and the entry count are cleared and the external slot retains its value. The [descriptor builders](#grouped-allocation-descriptor-builders) produce even strides. The producer of odd tagged strides remains open.

Build `cpu_bink_heap_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_aot_bink_group$'` for the source-built assembly control and its translation. For the inspected local DLL (SHA-256 `9eaf045d004cd7d6a8382aaff0c6ea23a29fec4ef8bfacd726b54f5bc26fafd0`), set `BINK_HEAP_ENTRIES` to `0x25bf0;0x25bc0` and `BINK_GROUP_ENTRY` to `0x25a40` alongside `BINK_HEAP_IMAGE`. Build `cpu_bink_package_lifted` and run `cpu_aot_bink_package_group`. The test supplies both descriptor arrays and runs the translated caller with the Windows test heap.

## Upscaler working-set counter updates

Immediately after the statistics calls at RVAs `0x74d2` and `0x75b1`, both paths add the new sample minus the previous sample to a 64-bit counter. A zero status substitutes zero for the new sample even if the supplied output word is nonzero. With starting counters 11 and 101, a previous sample of 23, a new sample of 70, successful status and flag 256, creation produces 58 and 148. Release produces 58 and 101 because it reads only the low flag byte. Both counters wrap modulo 2^64.

Build `cpu_working_set_delta_lifted` and run `ctest --preset dev --no-tests=error -R '^cpu_working_set_delta$'` for the source-built assembly control and its translation. With `FFX_UPSCALER_IMAGE` set to the fingerprinted local upscaler, build `ffx_package_working_set_delta` and run its namesake test for the two translated instruction ranges. The test supplies the status, samples and flags at the point where the query returns. The Xbox statistics provider, measurement units and real sample values remain unverified.

## Six API call sites

After loading the toolchain, inspect three local DLLs. `out/calls/six-apis.json` records image SHA-256, import slots, callers of thunks, embedded container sections and code references. Addresses below are RVAs in the relevant DLL. Indirect calls need execution evidence to identify the selected path.

```powershell
.\tools\dev.ps1
.\out\pe-env\Scripts\python.exe -B tools/call_evidence.py "game-files/<build-directory>"
```

| Import | Observed inputs, return handling and desktop check |
|---|---|
| `XMemAlloc` | At `0x25c35`, Bink's fallback requests the length plus 64 with attributes `0x05e50000` and checks for null. It advances the base by `64 - (base & 31)` bytes (33–64), giving a 32-byte-aligned user pointer. The two preceding bytes hold the offset at `[-1]` and a tag at `[-2]`. At `0x565d`, AMD rounds the nonzero request up to a multiple of 256, reads attributes at `0x6eee0` (on-disk initial value `0xa8004000`) and checks for null. Under the ERA layout the alignment fields give 32 and 256 bytes, matching this caller arithmetic. `era_heap_observed_words` checks the desktop handling of both words; allocator-ID and GPU-visibility semantics remain unconfirmed. |
| `XMemFree` | At `0x25be4`, Bink uses the saved offset to recover the allocation base and passes the same attributes. Custom allocations use a matching callback. AMD cleanup at `0x5712` and release at `0x5921` both use the allocation's attribute slot. The latter also clears the stored pointer. No observed caller consumes the return value. |
| `XMemVirtualQuery` | Four BVH callers pass the current process, target address and a zeroed 64-byte buffer. Each checks a nonzero return, bit `0x1000` of the word at `+0x20` and either low bit of the byte at `+0x30`. Source acceleration structure validation at `0x19c69` also requires the byte at `+0x24` to intersect mask `6`. Destination acceleration structure (`0x19ccb`), index buffer (`0x19d9d`) and vertex buffer (`0x19df9`) use the common checks. Diagnostics describe the whole check. The individual tail bits have no confirmed meaning. |
| `XMemGetWorkingSetStatistics` | Seven AMD calls pass selector `0x20000001` and a local output address. At `0x26b3` and elsewhere a zero return contributes zero to a calculation. Success uses the 64-bit value at `+8`. Code near `0x74d2/75b1` uses a before/after difference to update a counter. The units, scope and selector meaning remain open. |
| `D3D12SerializeVersionedRootSignature` | At `0xc2d0` the BVH code copies a caller's 48-byte descriptor and adds flags `0x03000000` at `+0x28`. At `0xc32f` it passes the descriptor, a blob output pointer and a null error output. Negative HRESULT enters diagnostics. Success uses the blob length and data pointer. A synthetic v1.1 descriptor returned `S_OK` with baseline flags and `E_INVALIDARG` with the two added bits. |
| `D3D12CreateRootSignatureDeserializer` | At `0x4f88`, AMD reads a byte pointer and a 32-bit length from record offsets `+0/+8`, then supplies an interface IID and object output address. A negative HRESULT or null object triggers partial cleanup. Success retrieves the descriptor via the object. Desktop code read the root signature from a complete Microsoft sample shader and returned `E_INVALIDARG` for a synthetic shader without one. |

### Graphics input paths

- The BVH serializer helper has 16 direct calls. At `0xd967–0xd998` the caller builds a version, six parameters and the parameter-array address on the stack. At `0x2151d` another path references container `0x25700` and passes it downstream with length `0x1134`. The DLL contains 52 code-referenced DXBC containers with structurally valid DXIL programs and `RTS0` sections. All 52 root signature headers carry `0x03000000`. The desktop failure case reproduces these flag bits. Their meaning remains unconfirmed.
- AMD registers the deserialization function in a callback table at `0xcea3–0xceaa`. A producer at `0xe9b8/0xe9c3` selects container `0x714f0` of length `0x1910`, copies its descriptor record and calls through the table at `0xebad`. The DLL has 29 structurally complete containers, each with a matching pointer-table entry and `RTS0` and `ACCC` sections. None contains DXIL. These observations support an embedded source. Runtime initialization determines the indirect path.
- Counts cover the inspected DLLs. Scene shaders, archive read order and the graphics path used at startup have no execution record here. An entry executable would allow its imports and initialization calls to be examined.

### Microsoft sample on desktop

The test uses Microsoft's SimpleTriangle at `Xbox-GDK-Samples` commit `b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The project includes a `Gaming.Xbox.Scarlett.x64` configuration and a published reference image. Desktop builds compile the two HLSL files using the sample's three vertices, colors, sRGB render target and clear color. The test checks SHA-256 for both shaders and the image. Set `SIMPLE_TRIANGLE_DIR` to that revision's sample directory and run `ctest --preset dev -R reference_triangle -V`. A sparse checkout into the default location needs no cache entry: `git clone --filter=blob:none --no-checkout https://github.com/microsoft/Xbox-GDK-Samples out/reference/b1e77b6ac10f95b07296025fda171ca2ddc42dbe`, then in that directory `git sparse-checkout set --no-cone /Samples/IntroGraphics/SimpleTriangle/` and `git checkout b1e77b6ac10f95b07296025fda171ca2ddc42dbe`. The sample repository's `.gitattributes` writes `*.hlsl` with CRLF. The test hashes the shaders with LF line endings, as they are stored in the repository, so a raw download and a checkout give the same result. [Microsoft sample overview](https://learn.microsoft.com/en-us/samples/microsoft/xbox-gdk-samples/simpletriangle/) · [Pinned source and reference image](https://github.com/microsoft/Xbox-GDK-Samples/tree/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/IntroGraphics/SimpleTriangle)

RTX 2060 and WARP rendered at 64×64. Excluding triangle edges by normalized coordinates left 10,812 color channels per adapter. Differences from the source-derived colors were at most 2 on an 8-bit scale. Against the published image the mean absolute difference was 6.88 and the maximum was 17. The two desktop images differed at 94 pixels, by at most 1 per channel. The published image came without a device model, build hash or lossless GPU readback. Source-derived colors supply the acceptance values. Microsoft distributes console GDKX separately. [Microsoft developer resources](https://developer.microsoft.com/en-us/games/resources/)

## Compute and ray tracing before executable intake

The inspection reads bytes from two local DLLs. Desktop parsers inspect serialized descriptors and DXIL. GPU tests execute HLSL built from this repository. After building, run:

```powershell
$pkg = (Get-ChildItem game-files -Directory)[0].FullName
.\out\pe-env\Scripts\python.exe -B tools/probe_roots.py $pkg --controls out/build/dev
ctest --preset dev -R 'compute_|dxr_query|shader_metadata|bvh_buffer_ops' -V
```

`out/calls/desktop-roots.json` records per-container return codes, root signature summaries, matching desktop fixtures, static call counts and validator diagnostics. It also hashes the input inventory and probe executable. Each container was checked independently.

| Windows check | XboxBvhBuilder | AMD upscaler |
|---|---|---|
| Both D3D12 deserializers read the embedded signature from the full container | 52/52 accepted, 16 distinct signatures | 29/29 accepted, 3 distinct signatures |
| Reserialize unmodified v1.1 descriptor | 52/52 returned `E_INVALIDARG` | 29/29 accepted |
| Clear only `0x03000000` in a memory copy and reserialize | 52/52 accepted. Console flag semantics are unknown. | Original flags were zero. |
| Validate the entire container and the internal bitcode separately using the Windows SDK | Both checks rejected all 52 Shader Model 6.5 compute programs. | `ACCC` present, no DXIL input. |

Both source-built control shaders passed container and module validation. For the BVH modules the validator reported an unrecognized `dxil-ms-xb2` target and `xbox.compiler.options` metadata. DXC disassembly showed 43 modules directly calling 33 distinct `xbox.` functions, including cache-controlled reads, cross-lane operations and floating-point atomics. The other nine had no such direct calls. Diagnostics retain up to 2 KiB per message. Counts use the complete disassembly. [DXC validator interface](https://github.com/microsoft/DirectXShaderCompiler/blob/main/include/dxc/dxcapi.h)

The first synthetic compute layout uses an SRV, a UAV and 16 root constants. Its `RTS0` payload matches 11 BVH containers byte for byte after clearing the two flags. The second uses 13 UAVs, 21 SRVs, four root CBVs and a static sampler. It matches 27 unmodified AMD containers. Both tests access the last registers in the descriptor tables. RTX 2060 and WARP wrote 257 correct integer words per layout and preserved 63 sentinel words.

The native DXR fixture builds a bottom-level structure (BLAS) from one triangle and a top-level structure (TLAS) from two instances. Five inline Shader Model 6.5 queries cover hits, a geometric miss, distance clipping, translation and instance masks. Both backends hit instances 7 and 11 at distance 1 and missed the other three rays. Integration of the game's acceleration structure layout and consumers with desktop DXR remains unverified. [Microsoft DXR specification](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html)

Four priority BVH containers use the first layout, are referenced by the function starting at `0x20270` and use 32×1×1 thread groups. Source-built HLSL reproduces the data operations inferred from static instructions. Independent CPU results supply the expected values.

| Container RVA | Observed input and write behavior |
|---|---|
| `0xb3ae0` | Copy the specified count of 16-byte input blocks to matching output offsets. |
| `0xb82e0` | Write the same constant to all four 32-bit words of each valid block. |
| `0xb4bd0` | Write consecutive unsigned 32-bit integers starting at a seed. |
| `0xb5be0` | Read the length parameter from the second input word and copy `64 × (parameter + 1)` bytes in steps. When reset is enabled, write the positive and negative maximum finite float bit patterns to six words at output offsets 16–39. |

Tests use disjoint buffers and cover zero length, thread-group boundaries, integer wraparound and untouched memory. Each desktop backend runs 26 cases and checks 384 words per case. The fourth operation schedules its optional reset in a second dispatch with a UAV barrier. Source cross-group synchronization, aliasing and actual game call parameters remain open.

### Module-only validation by DXC release

`shader_metadata_probe` validates each control shader twice through `root_probe`: the whole container with default flags, then the bare bitcode with `DxcValidatorFlags_ModuleOnly`. With the DXC 1.7.2308 in the original Windows SDK 10.0.26100 release, the second call returns `E_INVALIDARG` without a validation status. [Issue #98](https://github.com/R0Y00/Xbox2Windows/issues/98) The test still runs the root signature, container and disassembly checks. It reports a skip only when both module calls return that code and `dxil.dll` has a 101.x file version. `shader_metadata_controls` checks that rule without a validator and prints each case. A refusal from a 1.x validator, one refused and one validated shader in either order, another call failure and a failed module status are all rejected. The Linux Python job runs the same controls.

The table repeats `root_probe`'s two calls with the `dxcompiler.dll` and `dxil.dll` of each official x64 release. The inputs were `compute_root`, `compute_tables` and `triangle_vs` compiled by DXC 1.7.2308. Every release accepted the three containers. The calls ran under Wine 11.0. The 1.7.2308 and 1.9.2609 rows match the native Windows 10 results in #98. `IDxcVersionInfo` reports 1.8 on both sides of the boundary, so the test reads the file version instead.

| DXC release | `dxil.dll` file version | `IDxcVersionInfo` | Module-only call |
|---|---|---|---|
| 1.7.2308 | 101.7.2308.12 | 1.7 | `E_INVALIDARG` |
| 1.8.2403, 1.8.2405, 1.8.2407 | 101.8.2403.18, 101.8.2405.17, 101.8.2407.12 | 1.8 | `E_INVALIDARG` |
| 1.8.2502 | 1.8.2502.8 | 1.8 | Status 0 |
| 1.8.2505, 1.8.2505.1, 1.9.2602, 1.9.2602.24, 1.9.2607, 1.9.2609 | 1.x | 1.9 | Status 0 |

To check another release on Windows, configure a separate build directory with its compiler, then run the test:

```powershell
cmake --preset dev -B out/build/dxc-check -DDXC_EXECUTABLE=<path>\bin\x64\dxc.exe
cmake --build out/build/dxc-check
ctest --test-dir out/build/dxc-check --no-tests=error -R '^shader_metadata_probe$' -V
```

### Retargeting dxil-ms-xb2 modules to desktop DXIL

`tools/dxil_retarget.py` parses LLVM 3.7 bitcode blocks and records without modifying opaque subblocks such as function bodies. It rewrites `MODULE_CODE_TRIPLE` from `dxil-ms-xb2` to `dxil-ms-dx` and removes the `METADATA_NAME`/`METADATA_NAMED_NODE` pair for `xbox.compiler.options` in `METADATA_BLOCK_ID`, preserving numbered metadata indices. For a DXBC container it also canonicalizes the `DXIL` program header to `BitcodeOffset=16`, retargets any DXIL `STAT` reflection part, clears the `0x03000000` root signature flag pair in `RTS0` (unless `--keep-root-flags` is set) and zeroes the stale 16-byte DXBC digest. Desktop DXC rejects `dxil-ms` as an unknown triple; `dxil-ms-dx` is the accepted desktop DXIL triple.

On the local `XboxBvhBuilder.dll` (SHA-256 `99f803ac22c01ef329dddb1d40be67238d2a812d01e8d040b3cb5135bc23707e`; 52 SM 6.5 compute containers), 9 modules make no direct `xbox.*` calls (`0x25700`, `0x35720`, `0x3ab00`, `0x46880`, `0x92e30`, `0xb3ae0`, `0xb4bd0`, `0xb5be0`, `0xb82e0`). With `dxil.dll` 1.9.2602.17 through `root_probe`:

- Before retargeting, all 9 failed both container and `ModuleOnly` validation with `0x80aa0009` (`Unknown target triple 'dxil-ms-xb2'` and `Named metadata 'xbox.compiler.options' is unknown`).
- With `--keep-root-flags`, all 9 passed `ModuleOnly` (`0x00000000`, zero diagnostics) and all 9 failed container validation with `0x80aa0013` (`Unsupported bit-flag set (root signature flags 0x3000000)`).
- With default container retargeting (clearing `0x03000000`), all 9 passed both `ModuleOnly` and container validation (`0x00000000`, zero diagnostics).

The other 43 BVH modules still fail validation after retargeting because of direct `xbox.*` calls and aggregate `insertvalue`/`extractvalue` instructions used with those intrinsics. The 29 AMD upscaler containers carry `RTS0` and `ACCC` without a `DXIL` part. Re-signing containers for live `CreateComputePipelineState` execution remains separate work.

Public CI tests pure-Python bitcode/container controls (`dxil_retarget_controls`) and source-built `compute_root.cso` and `CopyBlocks.cso` shaders synthetically injected with `dxil-ms-xb2`, `xbox.compiler.options`, `BitcodeOffset=112` and `RTS0` flags `0x03000000` (`dxil_retarget_probe`), including negative controls for unmodified xb2, `--target-triple dxil-ms` and `--keep-root-flags`.

```powershell
ctest --preset dev --no-tests=error -R '^dxil_retarget_' -V
# Local validation of the 9 modules without xbox.* calls:
.\out\pe-env\Scripts\python.exe -B tools/dxil_retarget.py --pe "game-files/<build-directory>/XboxBvhBuilder.dll" --only-without-xbox-calls --probe out/build/dev/root_probe.exe --report out/calls/dxil-retarget.json
```

### BVH reads and cross-lane operations

In `out/calls/desktop-roots.json`, `static_operands` from `tools/probe_roots.py` groups functions by module count, call site and operand form. The counts below describe static instructions across the 52 BVH programs. Runtime frequency is unknown.

| Read form | Modules | Static calls |
|---|---:|---:|
| Global, one word | 17 | 227 |
| Global, two words | 3 | 129 |
| Global, three words | 12 | 184 |
| Global, four words | 10 | 44 |
| Scalar, one word | 16 | 32 |

Across 27 distinct modules, all 616 reads use 64-bit addresses and end with two LLVM `undef` arguments. They provide no definite cache policy. At RVA `0xa4780` a count read from base plus 12 limits active threads. A loop in `0xc6260` reads a word at base plus four times the index and writes it to the destination. The fixture exercises both data behaviors and the scalar offset `(index << 8) | 44`.

Two successive writes to the same GPU buffer are each ordered by a UAV-to-SRV transition before reads of all four widths. Each round submits 128 synthetic requests covering low-word carry, shared and scattered addresses, a final valid vector, overrun, misalignment and different high words. The CPU uses independent 64-bit range arithmetic and compares each returned word. Out-of-range sentinels are a fixture rule. Console cache hit rate, address ownership, invalid access behavior and visibility without explicit ordering remain unknown.

Eight modules contain 75 calls to `LaneSwizzle.i32`, with immediates 30, 60, 120, 240 and 480 occurring 15 times each. The `0x95e20` sequence conditionally accumulates exchanged values and reads lane 31. This motivated a candidate five-stage prefix sum within each 32-lane subgroup. Tests exercise native waves and group-shared memory separately. The logical 64-lane path also checks both halves of the ballot mask, prefix counts, dynamic indices and lane 31. The native path checks the active minimum, broadcast, divergent votes and dynamic reads. The CPU reconstructs expectations from GPU-reported native lane IDs.

Each backend runs 38 groups with empty, full and sparse ballots and 32 single-point inputs covering every position in each half. RTX 2060 reported native width 32–32 and WARP 4–4. Both passed the group-shared 64-logical-lane test and skipped forced native wave64 on capability grounds. A capable adapter will run that case. Single-point inputs, CPU prefix sums and final ray hits provide the PC acceptance checks. [WaveReadLaneAt semantics](https://learn.microsoft.com/en-us/windows/win32/direct3dhlsl/wavereadlaneat)

```powershell
ctest --preset dev -R 'bvh_reads|bvh_waves|bvh_wave64|bvh_hits' -V
```

### From buffer reads to ray hits

`bvh_hits_hardware` and `bvh_hits_warp` connect bounded address reads and half-group prefix sums to ray intersections. Box and triangle tests follow AMD GPURT's software approach. A synthetic root holds 64 child bounds with logical 64-bit triangle addresses. The 64 logical lanes test bounds and compact candidates using both half-group sums. Receiving lanes fetch vertices and reduce nearest hits within and across native waves. The fixture breaks distance ties by the smallest primitive ID. [AMD GPURT intersections](https://github.com/GPUOpen-Drivers/gpurt/blob/dev/src/shaders/IntersectCommon.hlsl)

Three successive scenes occupy the same GPU buffer. Each is written as a UAV and then read as an SRV. A CPU brute-force, double-precision plane intersection independently checks each ray's box count, triangle hit count, closest distance and primitive ID. Each round uses the same 132 rays, covering each leaf in isolation, forward, reverse and oblique rays, edges, vertices, zero distance and box-only intersections. Two rays lie in the first row's plane with a zero depth component. They enter the flat boxes through the zero-direction slab branch, one ending exactly on a box face, and never hit a triangle.

| Scene | Hits | Misses |
|---|---:|---:|
| Sparse layout, isolate all 64 leaves | 112 | 20 |
| Permuted addresses, overlapping geometry and equal-distance hits | 14 | 118 |
| Permuted again, with invalid address high bits, an out-of-range tail and a degenerate triangle | 108 | 24 |

RTX 2060 and WARP agreed with the CPU on every field. Closed intervals, tie breaking and invalid-leaf handling are fixture rules. The tests establish synchronized desktop reads and hit results. Scarlett node layouts and cache hints remain hypotheses to check against producer writes and consumer reads.

### Independent BVH checks on PC

Each scene sends the same 136 rays through a double-precision brute-force CPU test, a custom shader reading packed nodes, and standard DXR. DXR builds its own BLAS and TLAS from the original accessible vertices and runs inline queries. All paths check hit status and closest distance. The CPU also checks that the specific DXR-selected primitive intersects at that distance, allowing valid ties. Comparisons use triangle interiors and distance margins. Edge and endpoint cases use the preceding fixture's fixed conventions. [DXR ray extents](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-extents)

RTX 2060 and WARP each completed 408 comparisons. In fault controls only the custom shader's input changes. Swapping child 0 and 32 addresses changed four results, replaying the previous scene changed 102 and aliasing invalid address high bits changed one. Each fault must alter its designated witness ray. Run `ctest --preset dev -R bvh_differential -V` for hit counts, changed results and witness indices.

Project acceptance uses reproducible PC behavior. The next game-specific check is to identify the builder selected by the main executable, its node-buffer inputs and outputs, and the downstream queries, then add the observed constraints to PC tests.

### Building bounds under contention

Static disassembly identified 51 direct calls each to `xbox.global_atomic_fmin` and `xbox.global_atomic_fmax` across seven modules each. Each call takes a 64-bit address and a float after the opcode. Integer atomic add and subtract each occurred at four sites with a 64-bit address and a 32-bit operand. Regenerate `out/calls/desktop-roots.json` with `probe_roots.py` to inspect per-module counts and operand forms.

`bvh_atomics` builds seven sets of axis-aligned bounds in GPU memory. Contending threads use 32-bit compare-and-exchange to extend six raw float fields. Two counters cross the unsigned wrap boundary. The CPU checks every field and verifies that each accepted point received a unique prior value from each counter. Inputs cover positive-only and negative-only bounds, both signed zeros, subnormals, finite extremes, invalid points and an empty bucket. The fixture skips a point with any non-finite coordinate and orders negative zero before positive zero. These are explicit desktop conventions.

The query dispatch reads the accumulated bounds after a UAV barrier and tests 54 axis-aligned rays. The CPU computes each expected bucket mask by projecting the original points onto the three axes. RTX 2060 and WARP each passed 270 ray masks over five input batches, including the empty input and a reversed 1,025-point submission. Each full submission accepted 897 points. This exercises a source-built producer and consumer with shared bounds. The observed atomic calls alone do not identify the game's node layout or special-value rules.

### Packed leaves and initialization stores

The per-module `validation.ir.packing` report follows the immediate definition of the byte selector at all 106 `V_ALIGNBYTE_B32` sites. Each selector is masked with 3. The three modules contribute 44, 44 and 18 sites. Across seven other modules, 47 `BFI` sites include variable masks, zero and contiguous low-bit masks through 1023. The desktop test uses these constraints and AMD's bit selection and byte alignment operations to build a source-owned leaf format. Its field offsets and flag assignments are fixture conventions. [AMD RDNA 2 ISA reference](https://www.amd.com/content/dam/amd/en/documents/radeon-tech-docs/instruction-set-architectures/rdna2-shader-instruction-set-architecture.pdf)

`bvh_packed` uploads 64 packed leaf records per scene. A compute shader reconstructs their fields at four byte offsets, selects the ID and flags through a mask, then writes bounds, triangle vertices and logical addresses. The next dispatch traces the existing 132 rays directly against that buffer. A separate readback compares all 1,280 scene words plus four guard words per scene against the CPU layout. Three scenes cover address carry, an invalid high word, a truncated vertex span, degeneracy and ties. Flipping one selected ID bit in the first scene changed exactly four returned IDs. Distances and counts matched the independent geometry reference in both runs on RTX 2060 and WARP.

The static inventory records one scalar, six three-word and nine four-word cached stores. All have two trailing undefined operands. At module RVA `0xb92d0`, two pairs write the largest finite float three times, then its negative three times at the same base plus 12 bytes. `bvh_atomics` now initializes the six bounds on the GPU from dirty storage with this pair of stores. A UAV barrier orders initialization before accumulation. Empty buckets expose incomplete resets, and independent checks cover the two adjacent counters. Cache policy and the game's allocation layout remain unknown.

### Numeric operations on bounds

All 180 observed `Med3_F32` calls had two constant operands. In 156 calls they were the negative and positive largest finite floats. Six used the positive lower bound with float bits `0x0f000000` and the same upper bound. The remaining 18 used zero and `0x3f7ff972`. For ordered values these medians clamp the variable input to the two constants. The operand inventory now includes all 33 distinct direct Xbox intrinsic names.

`bvh_numeric` checks those three clamps at their boundaries, including neighboring float encodings and infinite inputs. It also tests three-input float extrema, three-input unsigned min/median/max and two independent packed binary16 lanes. For half precision the CPU decodes the exponent and fraction in double precision. Inputs cover every finite nonzero binary16 encoding and positive zero. The fixture maps signed zeros to positive zero and excludes non-finite half inputs. Each desktop backend agreed with the CPU on all 655,360 words from 65,536 input rows. NaN propagation and native Scarlett behavior remain open.

### Updating a live DXR scene

`dxr_update` moves a triangle from depth 1 to 3, changes two instance transforms and changes one visibility mask. It updates BLAS and TLAS in place using separate build and update scratch-size requirements. Seven rays cover old and new positions, visibility masks and distance clipping. Four change hit status. Both desktop backends matched the expected hit, instance, primitive and distance fields before and after the change. Each in-place result matched a fresh full build of the changed scene. [DirectX acceleration structure updates](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#acceleration-structure-update-constraints)

```powershell
ctest --preset dev -R 'bvh_packed|bvh_atomics|bvh_numeric|dxr_update|era_heap_caller_layout|static_exe_intake' -V
```

### Face culling under mirrored instances

`dxr_facing` sends 96 inline queries at eight instances of one two-triangle BLAS. Each instance receives twelve rays: both triangles, both directions along world z, and ray flags `RAY_FLAG_NONE`, `RAY_FLAG_CULL_BACK_FACING_TRIANGLES` and `RAY_FLAG_CULL_FRONT_FACING_TRIANGLES`. The instances combine `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_FRONT_COUNTERCLOCKWISE`, `D3D12_RAYTRACING_INSTANCE_FLAG_TRIANGLE_CULL_DISABLE`, non-uniform scale and mirroring on x, y or z. The DXR specification defines a triangle as front facing when its vertices appear clockwise from the ray origin in a left-handed frame, evaluates that winding in object space so a negative-determinant instance transform leaves facing unchanged, and makes both cull flags inert on a cull-disabled instance. [Ray flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#ray-flags) · [Instance flags](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#d3d12_raytracing_instance_flags)

The CPU reference in `tests/dxr_facing_reference.h` moves each ray into object space, intersects every triangle in double precision and applies those three rules. Before any GPU work the test compares it with a hand-derived table of front and back letters per instance. The reference expects 72 hits, 36 of them front facing, and 24 culled rays. A world-space winding test would disagree on 48 of the 96 rays, which all target the four mirrored instances. For each ray the shader reports hit status, `CommittedTriangleFrontFace`, instance ID, primitive index and distance. Status, facing, instance and primitive must match exactly. A hit distance of 2 or 3 may differ by 1e-5 relative, and a miss must leave all four words zero. Those 96 rays cross triangle interiors inside the distance interval. A separate CPU-only ray meets the z-scaled instance at the open upper endpoint of 2. Single-precision division in the inverse direction transform gave 1.9999999403953568 and incorrectly admitted the hit. The reference performs that division in double precision. A second fixture sends 12 rays through one identity instance of a square split into two consistently wound triangles in one BLAS geometry. Six rays cross its internal diagonal from opposite directions at three points. Each must commit a hit at distance 1 with the expected facing, and either incident primitive ID is valid. Four interior rays require a specific primitive and two rays outside the square must miss. The shader reports only the committed hit. Candidate multiplicity, silhouette edges, vertices and GPU distance endpoints remain unmeasured. [DXR shared-edge rules](https://microsoft.github.io/DirectX-Specs/d3d/Raytracing.html#fixed-function-ray-triangle-intersection-specification) The custom BVH shader has no facing rule and takes no part. The fixture skips below DXR tier 1.1 or Shader Model 6.5.

```powershell
ctest --preset dev --no-tests=error -R '^dxr_facing_' -V
```

### Desktop checks for the query tail and root flags

On x64 Windows, `desktop_memory_query` observed a 48-byte write from `VirtualQueryEx`. State and protection occupy offsets `0x20` and `0x24`. All 16 trailing sentinel bytes survived. With the caller's zero initialization, even a committed read/write page fails the tail check. The test supplies seven tail bytes after the query and compares the four callers' two predicate shapes on reserved, committed read/write, read-only and no-access pages. It also checks the zero-return path.

`bvh_root_roundtrip` uses a source-built compute shader with a v1.1 root signature. The desktop serializer rejected the added `0x03000000` bits with `E_INVALIDARG`. Clearing just those bits and serializing, decoding and serializing again produced the same bytes as the original control. RTX 2060 and WARP both created the root signature and pipeline from the control shader and recovered signature. Each dispatch matched 257 CPU reference words and preserved 63 guard words. This establishes the desktop behavior for that shader. The two flags' Scarlett semantics remain open.

`desktop_bvh_root_flags` asserts the same three cases with a synthetic v1.1 descriptor and no local inputs: baseline flags serialize, while `0x01000000`, `0x02000000` and the pair each return `E_INVALIDARG` with no blob produced. The suite therefore fails if a future SDK or driver starts accepting either console bit on its own.

### Creating root signatures on a second workstation

The second maintainer workstation ran Windows 26200.9974 with SDK 10.0.26100.0, an RTX 4060 Laptop GPU (driver 32.0.16.1656) and WARP (10.0.26100.9818). At commit `127fa86`, both backends passed 408 differential ray queries with the debug layer enabled. Forced native wave64 skipped on both. The hardware reported 32 native lanes and WARP reported 4. [Hardware report](https://github.com/R0Y00/Xbox2Windows/issues/1#issuecomment-7058907095)

The device probe groups containers in each DLL by the SHA-256 of their serialized root signature (`RTS0`). For one representative per group it deserializes the v1.1 descriptor, changes the flags in a memory copy, and records the serializer HRESULT. For each successful serialization it calls `CreateRootSignature` on each selected device and records that HRESULT separately.

| Local input | Containers / distinct layouts | Serializer and device results |
|---|---:|---|
| BVH builder | 52 / 16 | All 16 returned `E_INVALIDARG` in each of three cases: original flags, clear only `0x01000000`, or clear only `0x02000000`. Clearing both returned `S_OK` for serialization and device creation on both backends for every layout. |
| AMD upscaler | 29 / 3 | All three original layouts returned `S_OK` for serialization and device creation on both backends. |

The observed boundary is root signature creation. The desktop DXIL validator still rejected all 52 BVH programs, and the 29 upscaler containers contained no DXIL. An open binding test is the BVH layout shared by seven containers, including the one at RVA `0x99d60`. A source-built dispatch with CPU-checked readback would extend coverage beyond device creation for this layout.

After the first build with the Python dependencies, run the two source-built controls on WARP:

```powershell
ctest --preset dev -R '^root_device_controls_warp$' -V
```

With local inputs, generate the call inventory first. The runner verifies the DLL hashes and writes per-layout HRESULTs to `out/calls/root-devices.json`. Use `--warp-only` for a software-only report in `out/calls/root-devices-warp.json`.

```powershell
.\tools\dev.ps1
$pkg = 'game-files/<build-directory>'
$python = '.\out\pe-env\Scripts\python.exe'
& $python -B tools/call_evidence.py $pkg
& $python -B tools/probe_root_devices.py $pkg
```

### Dynamic dependencies at startup

Five `GetProcAddress` calls were inspected in the local AMD loader. The `0x10000` descriptor selects the packaged upscaler, whose five exports match. `0x20000` and `0x30000` select `amd_fidelityfx_framegeneration_xbox.dll`, absent from the inspected directory and torrent manifest. The loader checks for an already-loaded module before calling `LoadLibraryA`. Whether the main executable requests frame generation remains unknown. [AMD FidelityFX API](https://github.com/GPUOpen-LibrariesAndSDKs/FidelityFX-SDK/blob/main/Kits/FidelityFX/api/include/ffx_api.h)

The executable intake report's `dependency_paths` gives an explanatory path from the entry to each reachable dependency, preferring ordinary imports and marking delay imports and confirmed AMD branches. The AMD branch data applies only to a SHA-256-matched loader. New inventories include System32 forwarder chains. Older inventories provide first-hop evidence only. Dynamic lookups and execution order remain open. With DLLs alone, trace from each one:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/dependency_paths.py out/dependencies/pre-exe.json |
    Set-Content out/dependencies/pre-exe-paths.json -Encoding utf8
```

The conditional FidelityFX routes are pinned to one loader hash. When a reachable image has the loader's basename but a different SHA-256, the report adds an `unverified_loader_hash` finding that names both hashes, instead of returning no conditional edge and exiting successfully. `dependency_paths_controls` checks the pinned hash and a changed one with a synthetic inventory.

```powershell
.\out\pe-env\Scripts\python.exe -B tests/dependency_paths_check.py
```

To rehearse intake with the locally built candidate, keep its report in a separate directory:

```powershell
.\out\pe-env\Scripts\python.exe -B tools/intake_exe.py out/build/dev/intake_candidate.exe --reports out/intake-rehearsal
```

The tool reads the candidate as a file and uses the manifest slot as its graph name. The local rehearsal matched the candidate's SHA-256 to the fixture and preserved all 16 baseline DLL hashes. The fixture supplied ordinary imports by name and ordinal from the missing `intake_provider.dll`, a delay import from the missing `intake_delay.dll`, and an ordinary import of `ffxQuery` from the packaged AMD loader. From that loader, descriptor `0x10000` reached the packaged upscaler. Descriptors `0x20000` and `0x30000` reached the absent frame-generation module conditionally. The independent CTest uses a temporary package without that loader and checks that the report omits the frame-generation branch. The game's startup dependencies still require its own entry image.

## Resource chunks and shader formats

### State-linked settings in a script archive

`python -B tools/script_state_probe.py path/to/script.rpf --state <label>` checks aligned zlib pages, section lengths through CHKS, schema and array extents, and parent-to-child attribute ranges, and rejects a state node with more than one label-kind `id` attribute, as the replay does. It emits aggregate counts and at most one matching example. The supported profile has five root arrays. Other layouts and entries outside the recognized zlib framing require separate inspection.

In the local base script archive, 532 children of state nodes carried orbit distance, heading and pitch bounds, blend duration and priority together. Reformatting the stored integers, floats and vectors as decimal strings reproduced all five saved value hashes for each child. This establishes authored camera settings associated with states. Activation conditions, time units, interpolation and native calls still need execution evidence. The source-built regression changes a scalar while preserving its saved hash and separately introduces a parent cycle. Further synthetic trees cover a non-state parent, a missing camera attribute, a root with a parent, a state label that selects nothing, and an integer `id` attribute whose hash equals the label, which does not name a state (only label-kind IDs do, as in the replay). [CodeWalker field-name reference](https://github.com/dexyfex/CodeWalker/blob/master/CodeWalker.Core/GameFiles/MetaTypes/MetaNames.cs)

The Windows path replay locates a record by an anchor state ID. It consumes that record's node and attribute rows in memory. After building with the dev preset, run this command with the local base archive:

```powershell
python -B tools/script_path_replay.py path/to/script.rpf --host out/build/dev/script_path_host.dll --record-with bumper_f motorbike ./bumper_f ../bumper_r ../bumper_f car/bumper_f
```

Bare paths begin at the record's `states` container. A leading `./` starts at the active node and `../` moves to its parent. Each input is supplied by the experiment. The native host walks the stored first-child and sibling links, checks reverse links and selects a unique matching child ID. A second lookup uses parent indices to verify the resulting node. A missing or ambiguous input leaves the test host at its last selection. The local motorbike record yields distinct front and rear camera bounds and restores the original bounds on returning to the front. The runtime event that supplies the state ID remains unresolved.

### Drawing BC7 textures with a recompiled pixel shader

`linearize_4k_s_tail(tile, width, height, tail_level)` reads a single BC7 level from a shared GFX10 4 KiB mip-tail tile. Supply the decoded tile, the requested pixel dimensions and its zero-based position within the tail. Whole-tile storage and per-slot bounds are checked before reading. [AMD mip-tail address calculation](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10addrlib.cpp)

To test the published fixed-symbol rANS hypothesis against an isolated BCPack prefix, run `python -B tools/bcpack_probe.py packet.bin --blocks N` with an independently established block count for that stream. A chunk can contain several streams. A header match checks the predicted byte budget and initial state. A lane that reaches the expected terminal state without refills reports `verified_constant_lane`. When the supplied read masks assign every required refill byte, the probe decodes all symbols using those bytes. A result of `verified_constant_lane_with_supplied_refills` confirms that the lane reaches its terminal state under the proposed assignment. The zero-based `refill_symbol_indices` identify the symbols that consumed the bytes. An empty result leaves the lane models unresolved. This probe produces diagnostics only. [Microsoft BCPack modes](https://learn.microsoft.com/en-us/gaming/gdk/docs/reference/system/dstorage/enums/dstorage_bcpack_mode)

`tools/bc7_tiles.py` converts one padded GFX10 `SW_4K_S` surface of 16-byte BC7 blocks to desktop block rows. The caller supplies the pixel dimensions and the bytes of one mip level. Each 4 KiB tile contains 16 × 16 BC7 blocks. The address equation applies to the standard non-XOR mode. [AMD GFX10 swizzle tables](https://github.com/GPUOpen-Drivers/pal/blob/c5e800072a32f68b6ccc4422936d96167c6e0728/src/core/imported/addrlib/src/gfx10/gfx10SwizzlePattern.h)

The desktop check uploads those rows as `DXGI_FORMAT_BC7_UNORM`, runs the translated copy shader, and checks its floating-point readback against CPU-decoded RGBA values after half-precision export. Its case file starts with `BC7C` and two little-endian 32-bit pixel dimensions, followed by tightly packed BC7 block rows and one reference RGBA8 value per texel. The base texture dimensions must be divisible by four. Each resident mip uses its logical dimensions, up to 4096 on either axis and 4 Mi pixels total, with one reference value per texel. Even a 1×1 mip occupies a complete 16-byte BC7 block.

```powershell
ctest --preset dev -R '^bc7_draw_' -V
.\out\build\dev\offscreen_check.exe hardware --check-bc7 copy.cso base.case capture.rgba
.\out\build\dev\offscreen_check.exe hardware --check-bc7-mip1 copy.cso base.case mip1.case
.\out\build\dev\offscreen_check.exe hardware --check-bc7-tail copy.cso 3 mip3.case mip4.case mip5.case mip6.case mip7.case
```

The tail check uses one logical texture per draw. For a tail whose resident width or height has already reached one texel, specify the original logical dimensions with `--base-size WIDTH HEIGHT` immediately after the first resident mip index. Without this option the command shifts both first-resident dimensions left by that index. For a magnified fallback pixel exactly on a source texel boundary, the readback must match one complete neighboring RGBA texel. The same is accepted, per axis, for the next texel when the pixel center lies up to 1/512 texel below a boundary, tie included. The exact texel is compared first, the last texel has no such neighbor under clamp addressing, and a non-magnified draw accepts no neighbor. Observed: on the Windows 10 in-box WARP 10.0.19041.5794, drawing the 2×1 mip of a 4096×4 texture returned the second texel at 16 pixels of the 4096×4 target (x = 2044 to 2047 in each row), 4 of 2048×2 (x = 1022 and 1023), 1 of 1024×1 (x = 511) and 1 of 512×1 (x = 255), and at none of the smaller targets. The 4×4096 texture gave the same counts along y, as did the same texels uploaded as `R8G8B8A8_UNORM`. WARP 1.0.9 through 1.0.22 and an RTX 3060 on driver 596.36 returned the first texel at all of these pixels. Inferred: these positions are exactly those where rounding the scaled coordinate to the nearest multiple of 1/256 texel, ties upward, reaches the boundary. `D3D12_SUBTEXEL_FRACTIONAL_BIT_COUNT` in `d3d12.h` is 8, which fits this model but does not show how that WARP is implemented. Controls replace single pixels with a texel fetched at its center, so they give the same readback on every adapter: the next texel passes at pixel 2044 of 4096 and at the tie, pixel 255 of 512, and fails at pixels 2043 and 254. A mix of channels from both texels at pixel 2044, the previous texel at pixel 2048 and the first texel at the clamped edge pixel 4095 also fail. It uploads all supplied consecutive levels at their original subresource indices and sets the SRV's `MostDetailedMip` to the first supplied level. The command above takes a 64×64 first resident level and tests viewports from 512×512 down to 4×4. Point sampling at the three largest sizes must reproduce magnified pixels from the 64×64 reference. Each smaller viewport must match its own reference. The test uses committed resources and measures sampling correctness.

To check a proposed first read round in the BCPack probe, append `--prefetch-mask MASK`. Set bit *i* for each of the first eight lanes expected to read a byte in that round. The probe uses the proposed lane order to locate the byte and compares it with the last byte emitted by the reverse encoder. A matching first byte tests a single refill prediction. `--next-mask MASK` supplies the immediately following round in the same way. Each constant lane takes its next refill byte in every round in which its bit is set, so a lane that skips the first round takes its first refill byte from the second. A read beyond the lane's header budget is listed under `unbudgeted_read_offsets` instead of being ignored. Refill positions after these two rounds and the assignment of BC7 block fields require separate evidence.

The default header layout is interleaved at an eight-byte stride. Pass `--lane-stride 6` to read eight contiguous six-byte records, as proposed for BC7 BCPack streams in [#15](https://github.com/R0Y00/Xbox2Windows/issues/15). `--scan-lanes` prints each lane's mode, budget and state bytes without claiming a decode. The option changes only how the 48-byte header is read; the constant-lane model, the variable BC7 lane models and an independently known BC7 block remain separate questions.

`tools/bc7_blocks.py` supplies independently known BC7 blocks for [issue #15](https://github.com/R0Y00/Xbox2Windows/issues/15). `layout(mode, partition)` lists the name, first bit and width of every field in a 16-byte block, with bit 0 as the low bit of the first byte. `unpack(block)` and `pack(fields)` convert between a block and those named values, and `decode(block)` returns sixteen RGBA8 texels. Every bit of the eight valid modes belongs to one field, so `pack(unpack(block))` reproduces any such block. `fixture()` builds a 64×128 surface of 512 blocks. Each field value comes from a SHA-256 digest of the mode, case number and field name. It cycles through the modes and covers every partition, rotation and index selection value. The module contains no BCPack logic. It gives a candidate lane-to-field mapping the complete block rows and field values to compare against. [Microsoft BC7 format](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format)

```powershell
ctest --preset dev --no-tests=error -R '^bc7_block_reference$' -V
python -B tools/bc7_blocks.py out/bc7_fields.case --fields out/bc7_fields.json
```

The test compares the first bit of each field group with offsets transcribed from the published mode layouts, checks that every anchor texel lies in its subset, and decodes three blocks whose texels are worked out by hand in the test comments. It also decodes the mode-6 blocks of the draw fixture, which builds its own reference texels, and pins SHA-256 digests of the generated block rows and texels. When the optional `texture2ddecoder` package is installed, the test additionally compares 16,897 decoded blocks with that decoder. This comparison is skipped otherwise. The command above writes the fixture in the `BC7C` case layout. The `bc7_draw_warp` and `bc7_draw_hardware` checks also draw the 512-block fixture. WARP 10.0.26100.9818 and an RTX 5080 on driver 32.0.16.1692 each matched the CPU reference across 8,192 texels and 32,768 channels. Flipping the rotation bit of a mode-4 block while retaining the original reference produced the first mismatch at (16, 0).

The optional capture contains verified, top-down RGBA8 pixels converted from the GPU readback. The mip test binds both levels in one texture and renders to a half-size target so implicit derivatives select the lower mip. An original mode-6 fixture varies endpoints by block and indices by texel. The negative control shifts only the lower level's compressed blocks and requires a pixel mismatch. [Microsoft BC7 mode reference](https://learn.microsoft.com/en-us/windows/win32/direct3d11/bc7-format-mode-reference)

### Reading logical ranges and probing structured resources

`tools/paged_resource.py` reads a logical byte range from an open archive and a supplied segment map. Each segment gives a physical offset, stored byte count excluding padding, decoded byte count, and codec. Raw and zlib segments may be mixed. The reader verifies the complete compressed stream and decoded size before returning bytes from it. Reads that touch an unknown codec fail. Segments after it remain independently addressable.

Run `python -B tools/texture_descriptors.py <archive.ttpf>` to group descriptors in the first raw ETPF table by width, height, DXGI format number and mip-marker byte. The reader checks stored and logical bounds and reads up to 1 MiB of descriptors in batches of at most 4 KiB. It leaves texture payloads unread. The marker alone does not identify a resident mip. [Published descriptor layout](https://github.com/rpatty-dev/PercyStar/blob/1b710c5349ef1911d94092f19173e9813dfb300c/src/tex.rs#L748-L785)

Add `--raw-page-map INDEX` to report the logical and file offsets of a zero-based descriptor's first 4 KiB page. The interval must be raw. Equal stored and logical lengths require no padding check. For unequal lengths, the excess stored bytes must match the 12-bit trailing-byte count from the starting boundary. The command reads this suffix (up to 4,095 bytes) and verifies that each byte is zero. The command checks that boundary positions increase strictly, reads the boundary table in batches of at most 4 KiB, and accepts tables up to 1 MiB. It reads no texture payload.

Use `--zlib-segment INDEX` for a descriptor in a codec-8 interval. This mode reads and decompresses the selected interval, limiting both stored and decoded sizes to 1 MiB. It verifies the zlib checksum and end of stream, checks the decoded length against the declared extent, and requires any trailing bytes to be zero. The report contains the fields for a `Segment`, its logical start, and the descriptor page's offset within it. Decoded bytes are not included in the report.

Run `python -B tools/texture_descriptors.py --lookup-key 0x0123456789abcdef first.ttpf second.ttpf` to search the parallel little-endian 64-bit key tables in ETPF version 0x27a. Each match lists the archive path and zero-based descriptor index, in input order and then index order. Repeated keys produce separate matches. An absent key produces an empty list. The reader scans at most 512 KiB of keys per archive in batches of up to 4 KiB and leaves descriptor bodies and texture payloads unread. [Key table layout](https://github.com/holdmysocks/SixNG/blob/2f672555819f96993adeed3273653ad53eeb1d81/crates/sixng-core/src/ttpf.rs#L112-L123)

`tools/section_probe.py` samples up to 8 MiB at the start, middle and end of each input. It looks for 16-byte-aligned zlib streams, with stored and decoded sizes capped at 256 KiB. A section record must start and end with the requested tags, and its big-endian section sizes must exactly cover the decoded buffer. Streams cut off by a window or size cap are omitted. Counts establish record boundaries within sampled regions. Interpreting payload fields or validating a checksum inside a section requires its format.

```powershell
python -B tools/section_probe.py --first PSIN --last CHKS "game-files/<build-directory>/levels.rpf"
ctest --preset dev -R resource_range_controls -V
```

### Source-built binding, wave and texture checks

`dual_uav` dispatches through two root UAVs with a count supplied as a root constant. The CPU checks the input, active output and unwritten tail over seven counts. Decreasing the count from 129 to 128 leaves word 128 untouched. The wave tests compare a full-group mirror with a half-local mirror at lanes 31 and 32 and check an inclusive sum across both halves. With native wave64 support they also check a native mirror using the lane assignment returned by the GPU.

`texture_mips` writes an original RGBA8 checker pattern in padded 4×4 Morton tiles, decodes it into the device's copy footprints and point-samples every texel through a texture SRV. It checks all 148 texels of a 13×9 texture and its three smaller mips, plus a guard word after each level. Morton order is the synthetic input convention. Establishing a particular title's texture format and addressing mode requires separate evidence.

```powershell
ctest --preset dev -R 'dual_uav|texture_mips|bvh_waves|bvh_wave64' -V
```

### Optional compressed-record inventory

Run the following after building the native validator. The optional pass scans named records in the shader archive, decodes one raw-DEFLATE stream at a time and writes only aggregate metadata to the resource report. A candidate must fit the file, have matching stored sizes, reach the stream end, match the declared output length and contain a bounded DXBC section table with a DXIL program header. The pass caps stored and decoded spans at 8 MiB and 16 MiB. It records shader stages, section counts, LLVM bitcode framing, unused bytes at stream end and rejection reasons. The native validator samples the first accepted container per stage. These are content-based counts and depend on the recognized record format. [External record-format reference](https://github.com/ItsNotRexed/Leonida/blob/56f023a10a726da20702d4e121656ceda2391d93/tools/shader_carve.py)

Use `tools/extract_shaders.py --length-prefixed-names` with archives whose record names begin with a one-byte length that includes the NUL terminator. A printable length byte can also be a valid first character of a prefixless name. The extractor keeps the full printable run by default.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --scene-records --scene-validator out/build/dev/root_probe.exe
ctest --preset dev -R 'compressed_scene_metadata|static_exe_intake|desktop_memory_query|era_heap_failure' -V
```

The synthetic intake test compares the candidate's direct unresolved imports against a DLL baseline and against a second baseline with a peer caller. Each `entry_import_gaps` item records ordinary or delayed binding and whether the same module/symbol pair was already unresolved. Transitive and conditional routes remain in `dependency_paths`. The memory tests check a real one-byte Windows query failure and unsupported heap attribute bits in combination with a supported alignment.

### Raw archive scan

After building, use the source-built pixel shader as a known DXIL control.

```powershell
python -B tools/resource_inventory.py "game-files/<build-directory>" --shader-control out/build/dev/triangle_ps.cso
```

`out/resources/<build-directory>.json` records install chunks, flags, byte counts and missing layout destinations. It records file-header signatures for the build and scans all raw bytes in `appdata0.rpf`, `common.rpf` and `shaders.rpf` for format markers. Candidate DXBC containers are checked for length, section boundaries and DXIL program headers. The synthetic `dxcontainer_part_offsets` regression verifies that part-table entries can be enumerated independently of the physical layout, while rejecting overlapping, out-of-bounds, or duplicate-FourCC part ranges. Both the part map and DXIL scanner reject repeated FourCC tags even when those parts are physically separate; the regression includes a valid framed DXIL control to distinguish duplicate tags from missing shader payloads. It checks structural framing, not shader execution. `sample_streams` covers at most eight qualifying zlib streams per file within the first 4 MiB. Plaintext stays in memory. The sample cannot characterize the rest of the archive. Installation order and runtime I/O need independent checks. [DXIL container format](https://github.com/microsoft/DirectXShaderCompiler/blob/main/docs/DXIL.rst) · [RPF9 observation source](https://gtamodx.com/columns/eXk_fiBUaYta)
