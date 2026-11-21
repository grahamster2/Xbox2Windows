cmake_minimum_required(VERSION 3.21)
project(Xbox2Windows LANGUAGES CXX)

enable_testing()
add_executable(root_probe tools/root_probe.cpp)
target_compile_features(root_probe PRIVATE cxx_std_17)
target_link_libraries(root_probe PRIVATE d3d12)
add_executable(root_device_probe tools/root_device_probe.cpp)
target_compile_features(root_device_probe PRIVATE cxx_std_17)
target_link_libraries(root_device_probe PRIVATE d3d12 dxgi)
add_executable(d3d12_sdk_check tests/d3d12_sdk_check.cpp)
target_compile_features(d3d12_sdk_check PRIVATE cxx_std_17)
target_link_libraries(d3d12_sdk_check PRIVATE d3d12)
add_test(NAME desktop_root_signature COMMAND d3d12_sdk_check)
add_test(NAME desktop_bvh_root_flags COMMAND d3d12_sdk_check bvh-flags)

add_executable(era_heap_check tests/era_heap_check.cpp src/era_heap.cpp)
target_include_directories(era_heap_check PRIVATE src)
target_compile_features(era_heap_check PRIVATE cxx_std_20)
foreach(scenario IN ITEMS allocation failure concurrency caller_layout reuse observed_words large_alignment)
    add_test(NAME era_heap_${scenario} COMMAND era_heap_check ${scenario})
endforeach()

add_executable(memory_query_check tests/memory_query_check.cpp)
target_compile_features(memory_query_check PRIVATE cxx_std_17)
target_link_libraries(memory_query_check PRIVATE psapi)
add_test(NAME desktop_memory_query COMMAND memory_query_check)

add_executable(era_query_check tests/era_query_check.cpp src/era_query.cpp)
target_include_directories(era_query_check PRIVATE src)
target_compile_features(era_query_check PRIVATE cxx_std_17)
foreach(scenario IN ITEMS fields tail registry short)
    add_test(NAME era_query_${scenario} COMMAND era_query_check ${scenario})
endforeach()

# Title GPU address to desktop GPU virtual address table (no GPU needed).
add_executable(era_title_va_check tests/era_title_va_check.cpp src/era_title_va.cpp)
target_include_directories(era_title_va_check PRIVATE src)
target_compile_features(era_title_va_check PRIVATE cxx_std_17)
foreach(scenario IN ITEMS translate bounds registry shader)
    add_test(NAME era_title_va_${scenario} COMMAND era_title_va_check ${scenario})
endforeach()

# Desktop xmem.dll for the package DLLs' four named xmem imports.
add_library(xmem_shim SHARED src/xmem_shim.cpp src/xmem_shim.def src/era_heap.cpp src/era_query.cpp)
set_target_properties(xmem_shim PROPERTIES OUTPUT_NAME xmem)
target_include_directories(xmem_shim PRIVATE src)
target_compile_features(xmem_shim PRIVATE cxx_std_20)
add_executable(xmem_shim_check tests/xmem_shim_check.cpp)
target_link_libraries(xmem_shim_check PRIVATE xmem_shim)
target_compile_features(xmem_shim_check PRIVATE cxx_std_17)
foreach(scenario IN ITEMS exports allocation query working_set)
    add_test(NAME xmem_shim_${scenario} COMMAND xmem_shim_check ${scenario})
endforeach()

find_program(DXC_EXECUTABLE dxc REQUIRED)
get_filename_component(DXC_BIN_DIR "${DXC_EXECUTABLE}" DIRECTORY)
target_compile_definitions(root_probe PRIVATE SDK_DXC_DIR=L"${DXC_BIN_DIR}/")
add_executable(offscreen_check tests/offscreen_check.cpp tests/compute_check.cpp tests/dxr_check.cpp tests/buffer_ops.cpp)
target_compile_features(offscreen_check PRIVATE cxx_std_17)
target_link_libraries(offscreen_check PRIVATE d3d12 dxgi dxguid)
target_include_directories(offscreen_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
set(OFFSCREEN_AGILITY_620_DIR "" CACHE PATH "Agility SDK 1.620.2 x64 DLL directory for debug validation")
if(OFFSCREEN_AGILITY_620_DIR)
    if(EXISTS "${OFFSCREEN_AGILITY_620_DIR}/sdk.nupkg")
        set(agility_headers "${CMAKE_CURRENT_BINARY_DIR}/agility-headers")
        file(ARCHIVE_EXTRACT INPUT "${OFFSCREEN_AGILITY_620_DIR}/sdk.nupkg" DESTINATION "${agility_headers}"
             PATTERNS "build/native/include/*.h")
        target_include_directories(offscreen_check BEFORE PRIVATE "${agility_headers}/build/native/include")
    endif()
    foreach(dll IN ITEMS D3D12Core.dll d3d12SDKLayers.dll)
        configure_file("${OFFSCREEN_AGILITY_620_DIR}/${dll}" "${CMAKE_CURRENT_BINARY_DIR}/D3D12/${dll}" COPYONLY)
    endforeach()
    target_compile_definitions(offscreen_check PRIVATE OFFSCREEN_AGILITY_620)
endif()
set(OFFSCREEN_WARP_DIR "" CACHE PATH "Directory containing an app-local x64 d3d10warp.dll (Microsoft.Direct3D.WARP)")
if(OFFSCREEN_WARP_DIR)
    # On the Windows 10 in-box runtime, WARP 1.0.22 reports DXR tier 1.2 but every ray query misses.
    # With the Agility SDK runtime the same WARP passes the DXR checks, so require both together.
    if(NOT OFFSCREEN_AGILITY_620_DIR)
        message(FATAL_ERROR "OFFSCREEN_WARP_DIR requires OFFSCREEN_AGILITY_620_DIR. "
                "An app-local WARP on the in-box D3D12 runtime gives wrong ray query results.")
    endif()
endif()
include(cmake/app_local_warp.cmake)
offscreen_app_local_warp("${OFFSCREEN_WARP_DIR}" "${CMAKE_CURRENT_BINARY_DIR}")
add_test(NAME app_local_warp_toggle COMMAND "${CMAKE_COMMAND}" "-DSOURCE_DIR=${CMAKE_SOURCE_DIR}"
    "-DWORK_DIR=${CMAKE_CURRENT_BINARY_DIR}/app_local_warp_check" -P "${CMAKE_SOURCE_DIR}/tests/app_local_warp_check.cmake")
foreach(stage IN ITEMS vs ps)
    string(TOUPPER "${stage}" entry)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/triangle_${stage}.h")
    set(binary "${CMAKE_CURRENT_BINARY_DIR}/triangle_${stage}.cso")
    add_custom_command(OUTPUT "${header}" "${binary}"
        COMMAND "${DXC_EXECUTABLE}" -T ${stage}_6_0 -E ${entry}Main -Vn triangle_${stage}
                -Fh "${header}" -Fo "${binary}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/triangle.hlsl"
        DEPENDS tests/triangle.hlsl VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
set(ray_header "${CMAKE_CURRENT_BINARY_DIR}/ray_query.h")
set(pixel_vs "${CMAKE_CURRENT_BINARY_DIR}/pixel_aot_vs.h")
add_custom_command(OUTPUT "${pixel_vs}" COMMAND "${DXC_EXECUTABLE}" -T vs_6_0 -E main
    -Fh "${pixel_vs}" -Vn pixel_aot_vs "${CMAKE_CURRENT_SOURCE_DIR}/tests/pixel_aot_vs.hlsl"
    DEPENDS tests/pixel_aot_vs.hlsl VERBATIM)
target_sources(offscreen_check PRIVATE tests/pixel_aot_render.cpp "${pixel_vs}")
target_sources(offscreen_check PRIVATE tests/binding_mips.cpp tests/ffx_recurrent_views.cpp)
target_sources(offscreen_check PRIVATE tests/ffx_rcas_views.cpp)
foreach(storage IN ITEMS Native Packed)
    foreach(op IN ITEMS Read Write)
        set(symbol "Rcas${op}${storage}")
        set(header "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.h")
        add_custom_command(OUTPUT "${header}"
            COMMAND "${DXC_EXECUTABLE}" -T cs_6_0 -E "Rcas${op}" -D "STORAGE_${storage}=1"
                -Vn "${symbol}" -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.cso"
                "${CMAKE_CURRENT_SOURCE_DIR}/tests/ffx_rcas.hlsl"
            DEPENDS tests/ffx_rcas.hlsl src/rgb9e5.hlsli BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.cso" VERBATIM)
        target_sources(offscreen_check PRIVATE "${header}")
    endforeach()
endforeach()
foreach(entry IN ITEMS DualUav SampleMip WriteRecurrent)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${entry}.h")
    add_custom_command(OUTPUT "${header}" COMMAND "${DXC_EXECUTABLE}" -T cs_6_0 -E ${entry}
        -Vn ${entry} -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/binding_mips.hlsl" DEPENDS tests/binding_mips.hlsl
        BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso" VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
target_sources(offscreen_check PRIVATE tests/bvh_reads_waves.cpp tests/bvh_hits.cpp)
target_sources(offscreen_check PRIVATE tests/bvh_atomics.cpp tests/bvh_numeric.cpp)
# A title graphics allocation opened as a D3D12 heap and addressed through translated GPU addresses.
target_sources(offscreen_check PRIVATE tests/gpu_alias.cpp src/era_gpu_alias.cpp src/era_title_va.cpp src/era_heap.cpp)
# The same allocations read by a shader through 64-bit title addresses (src/title_va_lookup.hlsli).
target_sources(offscreen_check PRIVATE tests/gpu_alias_lookup.cpp)
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/LookupTitleAddresses.h" COMMAND "${DXC_EXECUTABLE}" -T cs_6_0
    -E LookupTitleAddresses -Vn LookupTitleAddresses -I "${CMAKE_CURRENT_SOURCE_DIR}/src"
    -Fh "${CMAKE_CURRENT_BINARY_DIR}/LookupTitleAddresses.h" -Fo "${CMAKE_CURRENT_BINARY_DIR}/LookupTitleAddresses.cso"
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/gpu_alias_lookup.hlsl" DEPENDS tests/gpu_alias_lookup.hlsl src/title_va_lookup.hlsli
    BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/LookupTitleAddresses.cso" VERBATIM)
target_sources(offscreen_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/LookupTitleAddresses.h")
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/WriteTitleAddresses.h" COMMAND "${DXC_EXECUTABLE}" -T cs_6_0
    -E WriteTitleAddresses -Vn WriteTitleAddresses -I "${CMAKE_CURRENT_SOURCE_DIR}/src"
    -Fh "${CMAKE_CURRENT_BINARY_DIR}/WriteTitleAddresses.h" -Fo "${CMAKE_CURRENT_BINARY_DIR}/WriteTitleAddresses.cso"
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/gpu_alias_writes.hlsl" DEPENDS tests/gpu_alias_writes.hlsl src/title_va_lookup.hlsli
    BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/WriteTitleAddresses.cso" VERBATIM)
target_sources(offscreen_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/WriteTitleAddresses.h")
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/ProbeArrayAtomics.h" COMMAND "${DXC_EXECUTABLE}" -T cs_6_0
    -E ProbeArrayAtomics -Vn ProbeArrayAtomics -I "${CMAKE_CURRENT_SOURCE_DIR}/src"
    -Fh "${CMAKE_CURRENT_BINARY_DIR}/ProbeArrayAtomics.h" -Fo "${CMAKE_CURRENT_BINARY_DIR}/ProbeArrayAtomics.cso"
    "${CMAKE_CURRENT_SOURCE_DIR}/tests/gpu_alias_writes.hlsl" DEPENDS tests/gpu_alias_writes.hlsl src/title_va_lookup.hlsli
    BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/ProbeArrayAtomics.cso" VERBATIM)
target_sources(offscreen_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/ProbeArrayAtomics.h")
target_include_directories(offscreen_check PRIVATE "${CMAKE_CURRENT_SOURCE_DIR}/src")
add_custom_command(OUTPUT "${CMAKE_CURRENT_BINARY_DIR}/AliasTransform.h" COMMAND "${DXC_EXECUTABLE}" -T cs_6_0
    -E AliasTransform -Vn AliasTransform -Fh "${CMAKE_CURRENT_BINARY_DIR}/AliasTransform.h"
    -Fo "${CMAKE_CURRENT_BINARY_DIR}/AliasTransform.cso" "${CMAKE_CURRENT_SOURCE_DIR}/tests/gpu_alias.hlsl"
    DEPENDS tests/gpu_alias.hlsl BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/AliasTransform.cso" VERBATIM)
target_sources(offscreen_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}/AliasTransform.h")
foreach(entry IN ITEMS InitBounds AtomicBounds QueryBounds NumericBounds)
    set(bounds_source tests/bvh_atomics.hlsl)
    if(entry STREQUAL "NumericBounds")
        set(bounds_source tests/bvh_numeric.hlsl)
    endif()
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${entry}.h")
    add_custom_command(OUTPUT "${header}" COMMAND "${DXC_EXECUTABLE}" -T cs_6_5 -E ${entry}
        -Vn ${entry} -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso"
        "${CMAKE_CURRENT_SOURCE_DIR}/${bounds_source}" DEPENDS "${bounds_source}"
        BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso" VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
foreach(entry IN ITEMS Produce ReadValues WaveValues Wave64 WriteScene ExpandLeaves TraceHits)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${entry}.h")
    set(wave_args -T cs_6_5 -E ${entry})
    if(entry STREQUAL "Wave64")
        set(wave_args -T cs_6_6 -E WaveValues -D REQUIRE_WAVE64=1)
    endif()
    add_custom_command(OUTPUT "${header}" COMMAND "${DXC_EXECUTABLE}" ${wave_args} -Vn ${entry}
        -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/bvh_reads_waves.hlsl" DEPENDS tests/bvh_reads_waves.hlsl
        BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso" VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
target_sources(offscreen_check PRIVATE tests/wave_shuffle.cpp)
foreach(width IN ITEMS 0 16 32 64)
    if(width)
        set(entry WaveShuffle${width})
        set(shuffle_args -T cs_6_6 -D WAVE_SIZE=${width})
    else()
        set(entry WaveShuffle)
        set(shuffle_args -T cs_6_5)
    endif()
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${entry}.h")
    add_custom_command(OUTPUT "${header}" COMMAND "${DXC_EXECUTABLE}" ${shuffle_args} -E Shuffle -Vn ${entry}
        -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/wave_shuffle.hlsl" DEPENDS tests/wave_shuffle.hlsl
        BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso" VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
foreach(entry IN ITEMS CopyBlocks FillBlocks NumberBlocks CopySpan ResetBounds)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${entry}.h")
    add_custom_command(OUTPUT "${header}" COMMAND "${DXC_EXECUTABLE}" -T cs_6_5 -E ${entry} -Vn ${entry}
        -Fh "${header}" -Fo "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso"
        "${CMAKE_CURRENT_SOURCE_DIR}/tests/buffer_ops.hlsl" DEPENDS tests/buffer_ops.hlsl
        BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${entry}.cso" VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
endforeach()
add_custom_command(OUTPUT "${ray_header}"
    COMMAND "${DXC_EXECUTABLE}" -T cs_6_5 -E CSMain -Vn ray_query -Fh "${ray_header}"
            -Fo "${CMAKE_CURRENT_BINARY_DIR}/ray_query.cso" "${CMAKE_CURRENT_SOURCE_DIR}/tests/ray_query.hlsl"
    BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/ray_query.cso" DEPENDS tests/ray_query.hlsl VERBATIM)
target_sources(offscreen_check PRIVATE "${ray_header}" tests/dxr_facing.cpp)
foreach(layout IN ITEMS root tables)
    set(header "${CMAKE_CURRENT_BINARY_DIR}/compute_${layout}.h")
    set(binary "${CMAKE_CURRENT_BINARY_DIR}/compute_${layout}.cso")
    set(defines)
    if(layout STREQUAL "tables")
        set(defines -D TABLE_BINDINGS=1)
    endif()
    add_custom_command(OUTPUT "${header}" "${binary}"
        COMMAND "${DXC_EXECUTABLE}" -T cs_6_0 -E CSMain ${defines} -Vn compute_${layout}
                -Fh "${header}" -Fo "${binary}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/compute.hlsl"
        DEPENDS tests/compute.hlsl VERBATIM)
    target_sources(offscreen_check PRIVATE "${header}")
    foreach(adapter IN ITEMS hardware warp)
        add_test(NAME compute_${layout}_${adapter} COMMAND offscreen_check ${adapter} --compute-${layout})
        set_tests_properties(compute_${layout}_${adapter} PROPERTIES TIMEOUT 45)
    endforeach()
    set_tests_properties(compute_${layout}_hardware PROPERTIES SKIP_RETURN_CODE 77)
endforeach()
add_executable(pso_root_check tests/pso_root_check.cpp)
target_compile_features(pso_root_check PRIVATE cxx_std_17)
target_include_directories(pso_root_check PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
target_link_libraries(pso_root_check PRIVATE d3d12 dxgi)
foreach(mode IN ITEMS embedded stripped)
    set(symbol pso_compute_${mode})
    set(header "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.h")
    set(strip_root)
    if(mode STREQUAL "stripped")
        set(strip_root -Qstrip_rootsignature)
    endif()
    add_custom_command(OUTPUT "${header}"
        COMMAND "${DXC_EXECUTABLE}" -T cs_6_0 -E CSMain ${strip_root} -Vn ${symbol}
                -Fh "${header}" "${CMAKE_CURRENT_SOURCE_DIR}/tests/compute.hlsl"
        DEPENDS tests/compute.hlsl VERBATIM)
    target_sources(pso_root_check PRIVATE "${header}")
endforeach()
add_test(NAME pso_root_signature_warp COMMAND pso_root_check)
foreach(adapter IN ITEMS hardware warp)
    add_test(NAME offscreen_${adapter} COMMAND offscreen_check ${adapter} triangle_${adapter}.bmp)
    add_test(NAME ffx_provider_loop_${adapter} COMMAND offscreen_check ${adapter} --ffx-provider-loop)
    add_test(NAME ffx_device_init_${adapter} COMMAND offscreen_check ${adapter} --ffx-device-init)
    add_test(NAME ffx_recurrent_${adapter} COMMAND offscreen_check ${adapter} --ffx-recurrent)
    add_test(NAME dual_uav_${adapter} COMMAND offscreen_check ${adapter} --dual-uav)
    add_test(NAME texture_mips_${adapter} COMMAND offscreen_check ${adapter} --texture-mips)
    set_tests_properties(dual_uav_${adapter} texture_mips_${adapter} PROPERTIES TIMEOUT 45)
    add_test(NAME dxr_query_${adapter} COMMAND offscreen_check ${adapter} --ray-query)
    add_test(NAME dxr_update_${adapter} COMMAND offscreen_check ${adapter} --dxr-update)
    set_tests_properties(dxr_update_${adapter} PROPERTIES TIMEOUT 45 SKIP_RETURN_CODE 77)
    add_test(NAME dxr_facing_${adapter} COMMAND offscreen_check ${adapter} --dxr-facing)
    set_tests_properties(dxr_facing_${adapter} PROPERTIES TIMEOUT 45 SKIP_RETURN_CODE 77)
    add_test(NAME bvh_buffer_ops_${adapter} COMMAND offscreen_check ${adapter} --buffer-ops)
    add_test(NAME gpu_alias_${adapter} COMMAND offscreen_check ${adapter} --gpu-alias)
    add_test(NAME gpu_alias_lookup_${adapter} COMMAND offscreen_check ${adapter} --gpu-alias-lookup)
    add_test(NAME gpu_alias_lookup_writes_${adapter} COMMAND offscreen_check ${adapter} --gpu-alias-lookup-writes)
    set_tests_properties(gpu_alias_${adapter} gpu_alias_lookup_${adapter} gpu_alias_lookup_writes_${adapter}
        PROPERTIES TIMEOUT 45 SKIP_RETURN_CODE 77)
    add_test(NAME bvh_root_roundtrip_${adapter} COMMAND offscreen_check ${adapter} --bvh-root-roundtrip)
    set_tests_properties(bvh_root_roundtrip_${adapter} PROPERTIES TIMEOUT 45)
    foreach(work IN ITEMS reads waves wave64 hits differential atomics numeric packed)
        add_test(NAME bvh_${work}_${adapter} COMMAND offscreen_check ${adapter} --bvh-${work})
        set_tests_properties(bvh_${work}_${adapter} PROPERTIES TIMEOUT 45)
    endforeach()
    set_tests_properties(bvh_wave64_${adapter} bvh_differential_${adapter} PROPERTIES SKIP_RETURN_CODE 77)
    add_test(NAME wave_shuffle_${adapter} COMMAND offscreen_check ${adapter} --wave-shuffle)
    set_tests_properties(wave_shuffle_${adapter} PROPERTIES TIMEOUT 45 SKIP_RETURN_CODE 77)
    set_tests_properties(bvh_buffer_ops_${adapter} PROPERTIES TIMEOUT 45)
    set_tests_properties(dxr_query_${adapter} PROPERTIES TIMEOUT 45 SKIP_RETURN_CODE 77)
    set_tests_properties(offscreen_${adapter} PROPERTIES TIMEOUT 45)
endforeach()
set_tests_properties(offscreen_hardware ffx_device_init_hardware ffx_recurrent_hardware ffx_provider_loop_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(dual_uav_hardware texture_mips_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(bvh_root_roundtrip_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(bvh_atomics_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(bvh_numeric_hardware bvh_packed_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(bvh_buffer_ops_hardware PROPERTIES SKIP_RETURN_CODE 77)
set_tests_properties(bvh_reads_hardware bvh_waves_hardware bvh_hits_hardware PROPERTIES SKIP_RETURN_CODE 77)
# The cs_6_5 fixtures skip on a WARP below SM 6.5. The Windows 10 in-box WARP reports SM 6.2.
set_tests_properties(bvh_buffer_ops_warp bvh_reads_warp bvh_waves_warp bvh_hits_warp bvh_packed_warp
    bvh_numeric_warp bvh_atomics_warp PROPERTIES SKIP_RETURN_CODE 77)

add_test(NAME offscreen_invalid_operation_warp COMMAND "${CMAKE_COMMAND}"
    "-DRENDERER=$<TARGET_FILE:offscreen_check>" -P "${CMAKE_SOURCE_DIR}/tests/offscreen_invalid_operation.cmake")
set_tests_properties(offscreen_invalid_operation_warp PROPERTIES TIMEOUT 45)

set(SIMPLE_TRIANGLE_DIR "${CMAKE_SOURCE_DIR}/out/reference/b1e77b6ac10f95b07296025fda171ca2ddc42dbe/Samples/IntroGraphics/SimpleTriangle"
    CACHE PATH "Microsoft SimpleTriangle sample directory")
if(EXISTS "${SIMPLE_TRIANGLE_DIR}/VertexShader.hlsl")
    find_program(REFERENCE_POWERSHELL NAMES pwsh powershell REQUIRED)
    foreach(adapter IN ITEMS hardware warp)
        add_test(NAME reference_triangle_${adapter} COMMAND "${REFERENCE_POWERSHELL}" -NoProfile -ExecutionPolicy Bypass
            -File "${CMAKE_SOURCE_DIR}/tests/reference_triangle.ps1" -Sample "${SIMPLE_TRIANGLE_DIR}"
            -Compiler "${DXC_EXECUTABLE}" -Renderer "$<TARGET_FILE:offscreen_check>" -Adapter ${adapter})
        set_tests_properties(reference_triangle_${adapter} PROPERTIES TIMEOUT 60 SKIP_RETURN_CODE 77)
    endforeach()
endif()

set(INTAKE_PYTHON "${CMAKE_SOURCE_DIR}/out/pe-env/Scripts/python.exe" CACHE FILEPATH
    "Native x64 Python with pefile for static intake tests")
target_sources(offscreen_check PRIVATE tests/ffx_device_init.cpp tests/ffx_backend_slots.cpp tests/ffx_recurrent.cpp tests/ffx_provider_loop.cpp)
set(FFX_UPSCALER_IMAGE "" CACHE FILEPATH "Local Xbox upscaler DLL for offline backend translation")
set(FFX_DISPATCH_SHADER_DIR "" CACHE PATH "Directory containing locally translated FSR4 shaders and scratch_reference.h")
if(FFX_DISPATCH_SHADER_DIR)
    if(NOT FFX_UPSCALER_IMAGE)
        message(FATAL_ERROR "Package dispatch replay requires FFX_UPSCALER_IMAGE")
    endif()
    target_compile_definitions(offscreen_check PRIVATE FFX_DISPATCH_AOT)
    target_include_directories(offscreen_check PRIVATE "${FFX_DISPATCH_SHADER_DIR}")
endif()
target_sources(offscreen_check PRIVATE tests/ffx_dispatch.cpp tests/ffx_dispatch_scratch.cpp)
foreach(stage IN ITEMS pass24 pass25 producer rcas)
    set(shader "${CMAKE_CURRENT_SOURCE_DIR}/tests/ffx_dispatch_control.hlsl")
    set(entry "${stage}")
    if(FFX_DISPATCH_SHADER_DIR)
        set(shader "${FFX_DISPATCH_SHADER_DIR}/${stage}.hlsl")
        set(entry main)
    endif()
    set(storage_formats Native Packed)
    if(stage MATCHES "^pass")
        set(storage_formats Raw)
    endif()
    foreach(storage IN LISTS storage_formats)
        set(symbol "${stage}${storage}")
        set(header "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.h")
        add_custom_command(OUTPUT "${header}"
            COMMAND "${DXC_EXECUTABLE}" -T cs_6_0 -E "${entry}" -D "STORAGE_${storage}=1"
                -I "${CMAKE_CURRENT_SOURCE_DIR}/tests" -Vn "${symbol}" -Fh "${header}"
                -Fo "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.cso" "${shader}"
            DEPENDS "${shader}" tests/ffx_dispatch_io.hlsli src/rgb9e5.hlsli
            BYPRODUCTS "${CMAKE_CURRENT_BINARY_DIR}/${symbol}.cso" VERBATIM)
        target_sources(offscreen_check PRIVATE "${header}")
    endforeach()
endforeach()
if(FFX_UPSCALER_IMAGE)
    if(NOT EXISTS "${INTAKE_PYTHON}" OR NOT EXISTS "${FFX_UPSCALER_IMAGE}")
        message(FATAL_ERROR "Package translation requires INTAKE_PYTHON and FFX_UPSCALER_IMAGE")
    endif()
    set(device_slice "${CMAKE_CURRENT_BINARY_DIR}/ffx_device_slice.cpp")
    add_custom_command(OUTPUT "${device_slice}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/ffx_device_init.py" "${FFX_UPSCALER_IMAGE}" "${device_slice}"
        DEPENDS "${FFX_UPSCALER_IMAGE}" tools/ffx_device_init.py tools/ffx_recurrent.py tools/ffx_provider_loop.py tools/ffx_dispatch_profile.py tools/ffx_version_query.py tools/cpu_ranges.py tools/cpu_leaf_aot.py
        VERBATIM)
    target_sources(offscreen_check PRIVATE "${device_slice}")
    target_include_directories(offscreen_check PRIVATE "${CMAKE_SOURCE_DIR}/tests")
    target_compile_definitions(offscreen_check PRIVATE FFX_PACKAGE_DEVICE_SLICE)
    get_filename_component(ffx_dir "${FFX_UPSCALER_IMAGE}" DIRECTORY)
    set(ffx_loader "${ffx_dir}/amd_fidelityfx_loader_xbox.dll")
    if(EXISTS "${ffx_loader}")
        set(loop_loader "${CMAKE_CURRENT_BINARY_DIR}/ffx_loop_loader.cpp")
        add_custom_command(OUTPUT "${loop_loader}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/ffx_create_context.py" "${ffx_loader}" "${loop_loader}"
            DEPENDS "${ffx_loader}" tools/ffx_create_context.py tools/ffx_version_query.py tools/cpu_leaf_aot.py tools/cpu_ranges.py
            VERBATIM)
        set_source_files_properties("${loop_loader}" PROPERTIES HEADER_FILE_ONLY TRUE)
        target_sources(offscreen_check PRIVATE "${loop_loader}" tests/ffx_provider_loader.cpp)
        target_compile_definitions(offscreen_check PRIVATE FFX_LOOP_LOADER)
    endif()
endif()
if(EXISTS "${INTAKE_PYTHON}")
    add_test(NAME docs_links COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/docs_links_check.py")
    # Outside a Git checkout, such as a source archive, the tracked-path check cannot run.
    set_tests_properties(docs_links PROPERTIES SKIP_RETURN_CODE 77)

    enable_language(ASM_MASM)
    function(cpu_asm_fixture image source)
        if(ARGC GREATER 2)
            set(exports "${ARGV2}")
        else()
            set(exports "${source}")
        endif()
        add_library(${image} SHARED tests/cpu/${source}.asm tests/cpu/${exports}.def)
        set_target_properties(${image} PROPERTIES LINKER_LANGUAGE CXX)
        target_link_options(${image} PRIVATE /NOENTRY /NODEFAULTLIB /INCREMENTAL:NO)
    endfunction()
    function(cpu_translation image translator entry)
        cmake_parse_arguments(BIND "FFX_DESTROY" "PROVIDER;SYMBOL;LINK_NAME;CALLBACK_OFFSET" "" ${ARGN})
        set(binding)
        if(BIND_FFX_DESTROY)
            list(APPEND binding --ffx-destroy)
        endif()
        if(DEFINED BIND_CALLBACK_OFFSET)
            list(APPEND binding --callback-offset "${BIND_CALLBACK_OFFSET}")
        endif()
        if(BIND_PROVIDER)
            set(binding --bind "$<TARGET_FILE:${BIND_PROVIDER}>" "${BIND_SYMBOL}")
            if(BIND_LINK_NAME)
                list(APPEND binding --link-name "${BIND_LINK_NAME}")
            endif()
        endif()
        set(lifted "${CMAKE_CURRENT_BINARY_DIR}/${image}_lifted.cpp")
        add_custom_command(OUTPUT "${lifted}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/${translator}.py"
                "$<TARGET_FILE:${image}>" ${entry} "${lifted}" ${binding}
            DEPENDS ${image} ${BIND_PROVIDER} tools/${translator}.py tools/cpu_leaf_aot.py tools/cpu_ranges.py VERBATIM)
        add_library(${image}_lifted SHARED "${lifted}")
        if(BIND_PROVIDER)
            target_link_libraries(${image}_lifted PRIVATE ${BIND_PROVIDER})
        endif()
    endfunction()
    foreach(variant IN ITEMS original inverted)
        cpu_asm_fixture(cpu_leaf_${variant} branch)
        cpu_translation(cpu_leaf_${variant} cpu_leaf_aot branch_value)
    endforeach()
    target_compile_definitions(cpu_leaf_inverted PRIVATE INVERT_BRANCH)
    add_test(NAME cpu_leaf_aot_branches COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/branches.py" "$<TARGET_FILE:cpu_leaf_original>"
        "$<TARGET_FILE:cpu_leaf_original_lifted>" "$<TARGET_FILE:cpu_leaf_inverted_lifted>")
    foreach(variant IN ITEMS original altered)
        cpu_asm_fixture(cpu_call_${variant} direct)
        cpu_translation(cpu_call_${variant} cpu_call_aot call_mix)
        # Keep the linked caller's rel32 directed at the callee itself for inspection.
        target_link_options(cpu_call_${variant}_lifted PRIVATE /INCREMENTAL:NO)
    endforeach()
    target_compile_definitions(cpu_call_altered PRIVATE ALTER_RETURN)
    foreach(variant IN ITEMS original altered)
        cpu_asm_fixture(cpu_repeat_${variant} repeat_call)
        cpu_translation(cpu_repeat_${variant} cpu_call_aot call_twice)
        target_link_options(cpu_repeat_${variant}_lifted PRIVATE /INCREMENTAL:NO)
    endforeach()
    target_compile_definitions(cpu_repeat_altered PRIVATE ALTER_RETURN)
    # Translation must reject this copy, which omits the trace-pointer reload.
    cpu_asm_fixture(cpu_repeat_dropped repeat_call)
    target_compile_definitions(cpu_repeat_dropped PRIVATE DROP_RELOAD)
    foreach(variant IN ITEMS original swapped first second)
        cpu_asm_fixture(cpu_pair_${variant} call_pair)
        cpu_translation(cpu_pair_${variant} cpu_call_aot call_pair)
        target_link_options(cpu_pair_${variant}_lifted PRIVATE /INCREMENTAL:NO)
    endforeach()
    target_compile_definitions(cpu_pair_swapped PRIVATE SWAP_CALLS)
    target_compile_definitions(cpu_pair_first PRIVATE ALTER_FIRST)
    target_compile_definitions(cpu_pair_second PRIVATE ALTER_SECOND)
    cpu_asm_fixture(cpu_register_probe register_probe)
    foreach(variant IN ITEMS original swapped)
        cpu_asm_fixture(cpu_switch_${variant} switch)
        cpu_translation(cpu_switch_${variant} cpu_switch_aot select_rva,select_rel,run_ops,shift_narrow,flag_select,cmov_read)
    endforeach()
    target_compile_definitions(cpu_switch_swapped PRIVATE SWAP_TABLE)
    add_test(NAME cpu_aot_jump_tables COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/switch_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_switch_original>" "$<TARGET_FILE:cpu_switch_original_lifted>"
        "$<TARGET_FILE:cpu_switch_swapped_lifted>")
    cpu_asm_fixture(cpu_switch_addr32 switch_addr32)
    cpu_translation(cpu_switch_addr32 cpu_switch_aot lea_wrap,lea_back,load_back,store_ahead)
    add_test(NAME cpu_aot_switch_addr32 COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/switch_addr32_check.py" "$<TARGET_FILE:cpu_switch_addr32>"
        "$<TARGET_FILE:cpu_switch_addr32_lifted>")
    cpu_asm_fixture(cpu_frames frames)
    cpu_translation(cpu_frames cpu_switch_aot save_nonvolatile,spill_reload,home_spill,join_frame,loop_counter)
    add_test(NAME cpu_aot_stack_frames COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/frames_check.py" check "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_frames>" "$<TARGET_FILE:cpu_frames_lifted>")
    cpu_asm_fixture(cpu_entry_order entry_order)
    cpu_translation(cpu_entry_order cpu_switch_aot square_tail,inc_square,pick_tail)
    add_test(NAME cpu_aot_entry_order COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/entry_order_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_entry_order>" "$<TARGET_FILE:cpu_entry_order_lifted>")
    cpu_asm_fixture(cpu_narrow narrow)
    cpu_translation(cpu_narrow cpu_switch_aot
        byte_merge,word_ops,byte_shift,byte_unary,al_less,mixed_width,ax_copy,zx_byte,set_less,set_zx,store_only,store_ret0,self_loop)
    add_test(NAME cpu_aot_narrow_returns COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/narrow_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_narrow>" "$<TARGET_FILE:cpu_narrow_lifted>")
    cpu_asm_fixture(cpu_fourth_arg fourth_arg)
    # ":4" gives an entry a fourth integer argument in R9; r9_scratch keeps the three-argument default.
    cpu_translation(cpu_fourth_arg cpu_switch_aot r9_mix:4,r9_loop:4,no_r9:4,r9_scratch)
    add_test(NAME cpu_aot_fourth_argument COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/fourth_arg_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_fourth_arg>" "$<TARGET_FILE:cpu_fourth_arg_lifted>"
        "${CMAKE_CURRENT_BINARY_DIR}/cpu_fourth_arg_lifted.cpp"
        "$<TARGET_FILE:cpu_narrow>" "$<TARGET_FILE:cpu_narrow_lifted>")
    cpu_asm_fixture(cpu_chained chained)
    cpu_translation(cpu_chained cpu_chained_aot choose_value)
    cpu_asm_fixture(cpu_chained_jne chained)
    target_compile_definitions(cpu_chained_jne PRIVATE USE_JNE)
    cpu_translation(cpu_chained_jne cpu_chained_aot choose_value)
    cpu_asm_fixture(cpu_chained_zero chained)
    target_compile_definitions(cpu_chained_zero PRIVATE USE_XOR_ZERO)
    cpu_translation(cpu_chained_zero cpu_chained_aot choose_value)
    cpu_asm_fixture(cpu_chained_imm chained)
    target_compile_definitions(cpu_chained_imm PRIVATE USE_IMM_REG)
    cpu_translation(cpu_chained_imm cpu_chained_aot choose_value)
    cpu_asm_fixture(cpu_callback callback)
    cpu_asm_fixture(cpu_version_query version_query)
    cpu_asm_fixture(cpu_heap_layout heap_layout)
    cpu_asm_fixture(cpu_bink_heap bink_heap)
    add_library(cpu_bink_heap_provider SHARED tests/cpu/bink_heap_provider.cpp src/era_heap.cpp)
    target_include_directories(cpu_bink_heap_provider PRIVATE src)
    target_link_libraries(cpu_bink_heap PRIVATE cpu_bink_heap_provider)
    function(bink_heap_translation target image dependency)
        set(cpp "${CMAKE_CURRENT_BINARY_DIR}/${target}.cpp")
        add_custom_command(OUTPUT "${cpp}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/bink_heap_aot.py"
                "${image}" "${cpp}" ${ARGN}
            DEPENDS ${dependency} tools/bink_heap_aot.py tools/ffx_version_query.py
                tools/cpu_leaf_aot.py tools/cpu_ranges.py VERBATIM)
        add_library(${target} SHARED "${cpp}")
        target_link_libraries(${target} PRIVATE cpu_bink_heap_provider)
    endfunction()
    bink_heap_translation(cpu_bink_heap_lifted "$<TARGET_FILE:cpu_bink_heap>" cpu_bink_heap --group-entry heap_group)
    add_test(NAME cpu_aot_bink_heap COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/bink_heap_check.py" "$<TARGET_FILE:cpu_bink_heap_provider>"
        "$<TARGET_FILE:cpu_bink_heap>" "$<TARGET_FILE:cpu_bink_heap_lifted>")
    add_test(NAME cpu_aot_bink_group COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/bink_group_check.py" "$<TARGET_FILE:cpu_bink_heap_provider>"
        "$<TARGET_FILE:cpu_bink_heap>" "$<TARGET_FILE:cpu_bink_heap_lifted>")
    set(BINK_GROUP_ENTRY "" CACHE STRING "Optional grouped allocation caller RVA in the local Bink image")
    set(BINK_HEAP_IMAGE "" CACHE FILEPATH "Local PE containing the Bink allocation and release callers")
    set(BINK_HEAP_ENTRIES "" CACHE STRING "Two semicolon-separated entry RVAs (allocation;release)")
    if(BINK_HEAP_IMAGE)
        list(LENGTH BINK_HEAP_ENTRIES entry_count)
        if(NOT entry_count EQUAL 2)
            message(FATAL_ERROR "Provide both BINK_HEAP_ENTRIES for the selected local image")
        endif()
        set(group_args)
        if(BINK_GROUP_ENTRY)
            set(group_args --group-entry "${BINK_GROUP_ENTRY}")
            add_test(NAME cpu_aot_bink_package_group COMMAND "${INTAKE_PYTHON}" -B
                "${CMAKE_SOURCE_DIR}/tests/cpu/bink_group_check.py" "$<TARGET_FILE:cpu_bink_heap_provider>"
                "$<TARGET_FILE:cpu_bink_package_lifted>")
        endif()
        bink_heap_translation(cpu_bink_package_lifted "${BINK_HEAP_IMAGE}" "${BINK_HEAP_IMAGE}"
            --entries ${BINK_HEAP_ENTRIES} ${group_args})
        add_test(NAME cpu_aot_bink_package_heap COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/bink_heap_check.py" "$<TARGET_FILE:cpu_bink_heap_provider>"
            "$<TARGET_FILE:cpu_bink_package_lifted>")
    endif()
    cpu_asm_fixture(cpu_working_set_delta working_set_delta)
    function(working_set_delta_translation target image dependency)
        set(cpp "${CMAKE_CURRENT_BINARY_DIR}/${target}.cpp")
        add_custom_command(OUTPUT "${cpp}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/ffx_working_set_delta.py"
                "${image}" "${cpp}" ${ARGN}
            DEPENDS ${dependency} tools/ffx_working_set_delta.py tools/ffx_version_query.py
                tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
        add_library(${target} SHARED "${cpp}")
    endfunction()
    working_set_delta_translation(cpu_working_set_delta_lifted "$<TARGET_FILE:cpu_working_set_delta>" cpu_working_set_delta)
    add_test(NAME cpu_working_set_delta COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/working_set_delta_check.py" "$<TARGET_FILE:cpu_working_set_delta>"
        "$<TARGET_FILE:cpu_working_set_delta_lifted>")
    if(FFX_UPSCALER_IMAGE)
        working_set_delta_translation(ffx_package_working_set_delta "${FFX_UPSCALER_IMAGE}" "${FFX_UPSCALER_IMAGE}" --package)
        add_test(NAME ffx_package_working_set_delta COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/working_set_delta_check.py" "$<TARGET_FILE:ffx_package_working_set_delta>")
    endif()
    cpu_asm_fixture(cpu_bink_descriptors bink_descriptors)
    function(bink_descriptors_translation target image dependency)
        set(cpp "${CMAKE_CURRENT_BINARY_DIR}/${target}.cpp")
        add_custom_command(OUTPUT "${cpp}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/bink_descriptors_aot.py"
                "${image}" "${cpp}" --frame-planes ${ARGN}
            DEPENDS ${dependency} tools/bink_descriptors_aot.py tools/bink_heap_aot.py
                tools/ffx_version_query.py tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
        add_library(${target} SHARED "${cpp}")
    endfunction()
    bink_descriptors_translation(cpu_bink_descriptors_lifted "$<TARGET_FILE:cpu_bink_descriptors>" cpu_bink_descriptors)
    add_test(NAME cpu_bink_descriptors COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/bink_descriptors_check.py" --native "$<TARGET_FILE:cpu_bink_descriptors>"
        "$<TARGET_FILE:cpu_bink_descriptors_lifted>")
    add_test(NAME cpu_bink_frame_planes COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/bink_frame_planes_check.py" "$<TARGET_FILE:cpu_bink_descriptors>"
        "$<TARGET_FILE:cpu_bink_descriptors_lifted>")
    set(BINK_DESCRIPTOR_IMAGE "" CACHE FILEPATH "Local Bink DLL for the grouped descriptor producer experiment")
    if(BINK_DESCRIPTOR_IMAGE)
        bink_descriptors_translation(cpu_bink_package_descriptors "${BINK_DESCRIPTOR_IMAGE}" "${BINK_DESCRIPTOR_IMAGE}" --package)
        add_test(NAME cpu_bink_package_frame_planes COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/bink_frame_planes_check.py" "$<TARGET_FILE:cpu_bink_package_descriptors>")
        add_test(NAME cpu_bink_package_descriptors COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/bink_descriptors_check.py" "$<TARGET_FILE:cpu_bink_package_descriptors>")
    endif()
    function(bink_frame_integration name driver descriptors heap)
        # Link identical translated objects to the shared DLL and to the recording
        # provider so a failing allocation can be injected at the import boundary.
        add_library(${name}_objects OBJECT "${driver}" "${CMAKE_CURRENT_BINARY_DIR}/${descriptors}.cpp"
            "${CMAKE_CURRENT_BINARY_DIR}/${heap}.cpp")
        add_library(${name} SHARED $<TARGET_OBJECTS:${name}_objects>)
        target_link_libraries(${name} PRIVATE xmem_shim)
        add_library(${name}_observed SHARED $<TARGET_OBJECTS:${name}_objects>)
        target_link_libraries(${name}_observed PRIVATE cpu_bink_heap_provider)
        add_test(NAME ${name} COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/bink_frame_heap_check.py" "$<TARGET_FILE:${name}>"
            "$<TARGET_FILE:${name}_observed>" "$<TARGET_FILE:xmem_shim>" "$<TARGET_FILE:cpu_bink_heap_provider>")
    endfunction()
    bink_frame_integration(cpu_bink_frame_to_heap tests/cpu/bink_frame_control.cpp
        cpu_bink_descriptors_lifted cpu_bink_heap_lifted)
    if(BINK_DESCRIPTOR_IMAGE AND BINK_DESCRIPTOR_IMAGE STREQUAL BINK_HEAP_IMAGE AND BINK_GROUP_ENTRY)
        set(frame_driver "${CMAKE_CURRENT_BINARY_DIR}/bink_frame_driver.cpp")
        add_custom_command(OUTPUT "${frame_driver}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/bink_frame_aot.py"
                "${BINK_DESCRIPTOR_IMAGE}" "${frame_driver}"
            DEPENDS "${BINK_DESCRIPTOR_IMAGE}" tools/bink_frame_aot.py tools/bink_descriptors_aot.py
                tools/bink_heap_aot.py tools/ffx_version_query.py tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
        bink_frame_integration(cpu_bink_package_frame_to_heap "${frame_driver}"
            cpu_bink_package_descriptors cpu_bink_package_lifted)
    endif()
    set(heap_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_heap_layout_lifted.cpp")
    add_custom_command(OUTPUT "${heap_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/heap_layout.py"
            emit "$<TARGET_FILE:cpu_heap_layout>" "${heap_fixture}"
        DEPENDS cpu_heap_layout tests/cpu/heap_layout.py tools/ffx_version_query.py
            tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
    add_library(cpu_heap_layout_lifted SHARED "${heap_fixture}")
    add_test(NAME cpu_aot_heap_layout COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/heap_layout.py"
        check "$<TARGET_FILE:cpu_heap_layout>" "$<TARGET_FILE:cpu_heap_layout_lifted>")
    cpu_asm_fixture(cpu_shift_count shift_count)
    set(shift_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_shift_count_lifted.cpp")
    add_custom_command(OUTPUT "${shift_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/shift_count.py"
            emit "$<TARGET_FILE:cpu_shift_count>" "${shift_fixture}"
        DEPENDS cpu_shift_count tests/cpu/shift_count.py tools/ffx_version_query.py
            tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
    add_library(cpu_shift_count_lifted SHARED "${shift_fixture}")
    add_test(NAME cpu_aot_shift_count COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/shift_count.py"
        check "$<TARGET_FILE:cpu_shift_count>" "$<TARGET_FILE:cpu_shift_count_lifted>")
    set(query_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_version_query_lifted.cpp")
    add_custom_command(OUTPUT "${query_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/version_query_fixture.py"
            emit "$<TARGET_FILE:cpu_version_query>" "${query_fixture}"
        DEPENDS cpu_version_query tests/cpu/version_query_fixture.py tools/ffx_version_query.py
            tools/cpu_ranges.py tools/cpu_leaf_aot.py tests/cpu/version_query_check.py VERBATIM)
    add_library(cpu_version_query_lifted SHARED "${query_fixture}")
    add_test(NAME cpu_aot_version_query COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/version_query_fixture.py" check "$<TARGET_FILE:cpu_version_query>"
        "$<TARGET_FILE:cpu_version_query_lifted>")
    add_test(NAME cpu_lower_prefixes COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/lower_prefixes_check.py")
    cpu_asm_fixture(cpu_address_size address_size)
    set(address_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_address_size_lifted.cpp")
    add_custom_command(OUTPUT "${address_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/address_size.py"
            emit "$<TARGET_FILE:cpu_address_size>" "${address_fixture}"
        DEPENDS cpu_address_size tests/cpu/address_size.py tools/ffx_version_query.py
            tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
    add_library(cpu_address_size_lifted SHARED "${address_fixture}")
    add_test(NAME cpu_aot_address_size COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/address_size.py"
        check "$<TARGET_FILE:cpu_address_size>" "$<TARGET_FILE:cpu_address_size_lifted>")
    cpu_asm_fixture(cpu_bmi1 bmi1)
    set(bmi1_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_bmi1_lifted.cpp")
    add_custom_command(OUTPUT "${bmi1_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/bmi1.py"
            emit "$<TARGET_FILE:cpu_bmi1>" "${bmi1_fixture}"
        DEPENDS cpu_bmi1 tests/cpu/bmi1.py tools/ffx_version_query.py
            tools/cpu_ranges.py tools/cpu_leaf_aot.py VERBATIM)
    add_library(cpu_bmi1_lifted SHARED "${bmi1_fixture}")
    add_test(NAME cpu_aot_bmi1 COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/bmi1.py"
        check "$<TARGET_FILE:cpu_bmi1>" "$<TARGET_FILE:cpu_bmi1_lifted>")
    set_tests_properties(cpu_aot_bmi1 PROPERTIES SKIP_RETURN_CODE 77)
    cpu_translation(cpu_callback cpu_chained_aot call_context CALLBACK_OFFSET 8)
    cpu_asm_fixture(cpu_destroy destroy)
    target_link_libraries(cpu_destroy PRIVATE ucrt.lib)
    cpu_translation(cpu_destroy cpu_chained_aot release_context FFX_DESTROY)
    add_library(cpu_destroy_provider SHARED tests/cpu/destroy_provider.cpp)
    cpu_asm_fixture(cpu_create create_context)
    set(create_fixture "${CMAKE_CURRENT_BINARY_DIR}/cpu_create_lifted.cpp")
    add_custom_command(OUTPUT "${create_fixture}"
        COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/cpu/create_fixture.py"
            "$<TARGET_FILE:cpu_create>" "${create_fixture}"
        DEPENDS cpu_create tests/cpu/create_fixture.py tools/ffx_create_context.py
            tools/ffx_version_query.py tools/cpu_leaf_aot.py tools/cpu_ranges.py VERBATIM)
    add_library(cpu_create_lifted SHARED "${create_fixture}")
    add_test(NAME cpu_aot_context_lifecycle COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/context_lifecycle.py" "$<TARGET_FILE:cpu_create>"
        "$<TARGET_FILE:cpu_create_lifted>" "$<TARGET_FILE:cpu_destroy_lifted>")
    add_test(NAME cpu_aot_destroy_contract COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/destroy_check.py" "$<TARGET_FILE:cpu_destroy>"
        "$<TARGET_FILE:cpu_destroy_lifted>" "$<TARGET_FILE:cpu_destroy_provider>")
    add_test(NAME cpu_aot_object_callback COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/callback_check.py" "$<TARGET_FILE:cpu_callback>"
        "$<TARGET_FILE:cpu_callback_lifted>")
    add_test(NAME cpu_aot_chained_ranges COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/chained_check.py" "$<TARGET_FILE:cpu_chained>"
        "$<TARGET_FILE:cpu_chained_lifted>" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_chained_jne>" "$<TARGET_FILE:cpu_chained_jne_lifted>"
        "$<TARGET_FILE:cpu_chained_zero>" "$<TARGET_FILE:cpu_chained_zero_lifted>"
        "$<TARGET_FILE:cpu_chained_imm>" "$<TARGET_FILE:cpu_chained_imm_lifted>")
    add_test(NAME cpu_aot_direct_call COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/direct_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_call_original>" "$<TARGET_FILE:cpu_call_original_lifted>"
        "$<TARGET_FILE:cpu_call_altered_lifted>")
    add_test(NAME cpu_aot_call_pair COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/call_pair_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_pair_original>" "$<TARGET_FILE:cpu_pair_swapped>"
        "$<TARGET_FILE:cpu_pair_original_lifted>" "$<TARGET_FILE:cpu_pair_swapped_lifted>"
        "$<TARGET_FILE:cpu_pair_first_lifted>" "$<TARGET_FILE:cpu_pair_second_lifted>")
    foreach(kind IN ITEMS named ordinal)
        if(kind STREQUAL "named")
            set(prefix cpu_import)
            set(contract SYMBOL mix_args)
        else()
            set(prefix cpu_ordinal)
            set(contract SYMBOL "#77" LINK_NAME mix_args)
        endif()
        foreach(variant IN ITEMS provider altered)
            add_library(${prefix}_${variant} SHARED tests/cpu/import_provider.cpp tests/cpu/${kind}_provider.def)
        endforeach()
        target_compile_definitions(${prefix}_altered PRIVATE ALTER_RETURN)
        cpu_asm_fixture(${prefix}_original direct import)
        target_compile_definitions(${prefix}_original PRIVATE IMPORT_MIX)
        target_link_libraries(${prefix}_original PRIVATE ${prefix}_provider)
        cpu_translation(${prefix}_original cpu_call_aot call_mix PROVIDER ${prefix}_provider ${contract})
        target_link_options(${prefix}_original_lifted PRIVATE /INCREMENTAL:NO)
    endforeach()
    cpu_asm_fixture(cpu_repeat_import repeat_call repeat_import)
    target_compile_definitions(cpu_repeat_import PRIVATE IMPORT_MIX)
    target_link_libraries(cpu_repeat_import PRIVATE cpu_import_provider)
    cpu_translation(cpu_repeat_import cpu_call_aot call_twice PROVIDER cpu_import_provider SYMBOL mix_args)
    target_link_options(cpu_repeat_import_lifted PRIVATE /INCREMENTAL:NO)
    add_test(NAME cpu_aot_repeated_call COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/repeat_call_check.py" "$<TARGET_FILE:cpu_register_probe>"
        "$<TARGET_FILE:cpu_repeat_original>" "$<TARGET_FILE:cpu_repeat_original_lifted>"
        "$<TARGET_FILE:cpu_repeat_altered_lifted>" "$<TARGET_FILE:cpu_repeat_dropped>"
        "$<TARGET_FILE:cpu_repeat_import>" "$<TARGET_FILE:cpu_repeat_import_lifted>")
    add_test(NAME cpu_aot_import_call COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/import_check.py" "$<TARGET_FILE:cpu_import_original>"
        "$<TARGET_FILE:cpu_import_original_lifted>" "$<TARGET_FILE:cpu_import_provider>"
        "$<TARGET_FILE:cpu_import_altered>" "$<TARGET_FILE:cpu_register_probe>")
    add_library(cpu_exe_io SHARED tests/cpu/exe_io.cpp)
    foreach(variant IN ITEMS original inverted)
        set(native cpu_exe_${variant})
        set(lifted ${native}_lifted)
        add_executable(${native} tests/cpu/exe_entry.asm tests/cpu/direct.asm)
        set_target_properties(${native} PROPERTIES LINKER_LANGUAGE CXX)
        target_compile_definitions(${native} PRIVATE IMPORT_MIX)
        if(variant STREQUAL "inverted")
            target_compile_definitions(${native} PRIVATE FLIP_SELECTOR)
        endif()
        target_link_options(${native} PRIVATE /ENTRY:native_entry /NODEFAULTLIB /INCREMENTAL:NO)
        set(source "${CMAKE_CURRENT_BINARY_DIR}/${lifted}.cpp")
        add_custom_command(OUTPUT "${source}"
            COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tools/cpu_exe_aot.py"
                "$<TARGET_FILE:${native}>" "$<TARGET_FILE:cpu_exe_io>"
                "$<TARGET_FILE:cpu_import_provider>" "${source}"
            DEPENDS ${native} cpu_exe_io cpu_import_provider tools/cpu_exe_aot.py
                tools/cpu_call_aot.py tools/cpu_leaf_aot.py tools/cpu_ranges.py tools/ffx_version_query.py
            VERBATIM)
        add_executable(${lifted} "${source}")
        target_link_options(${lifted} PRIVATE /INCREMENTAL:NO)
        foreach(program IN ITEMS ${native} ${lifted})
            target_link_libraries(${program} PRIVATE cpu_exe_io cpu_import_provider)
        endforeach()
    endforeach()
    add_test(NAME cpu_aot_exe_entry COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/exe_entry_check.py"
        "$<TARGET_FILE:cpu_exe_original>" "$<TARGET_FILE:cpu_exe_original_lifted>"
        "$<TARGET_FILE:cpu_exe_inverted>" "$<TARGET_FILE:cpu_exe_inverted_lifted>"
        "$<TARGET_FILE:cpu_exe_io>" "$<TARGET_FILE:cpu_import_provider>")
    add_test(NAME cpu_aot_ordinal_import COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/import_check.py" "$<TARGET_FILE:cpu_ordinal_original>"
        "$<TARGET_FILE:cpu_ordinal_original_lifted>" "$<TARGET_FILE:cpu_ordinal_provider>"
        "$<TARGET_FILE:cpu_ordinal_altered>" "$<TARGET_FILE:cpu_register_probe>"
        --ordinal-control "$<TARGET_FILE:cpu_import_provider>")
    cpu_asm_fixture(cpu_reloc_original reloc_route)
    target_link_libraries(cpu_reloc_original PRIVATE cpu_import_provider)
    cpu_asm_fixture(cpu_two_imports two_imports)
    target_link_libraries(cpu_two_imports PRIVATE cpu_import_provider cpu_register_probe)
    add_library(pe_map SHARED tools/pe_map.cpp)
    add_test(NAME cpu_native_exe_chain COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/mapped_exe_chain_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_exe_original>" "$<TARGET_FILE:cpu_exe_original_lifted>"
        "$<TARGET_FILE:cpu_exe_io>" "$<TARGET_FILE:cpu_import_provider>")
    target_compile_features(pe_map PRIVATE cxx_std_17)
    add_test(NAME cpu_native_map COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_map_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_register_probe>"
        --named "$<TARGET_FILE:cpu_import_original>" "$<TARGET_FILE:cpu_import_original_lifted>"
            "$<TARGET_FILE:cpu_import_provider>" "$<TARGET_FILE:cpu_import_altered>"
        --ordinal "$<TARGET_FILE:cpu_ordinal_original>" "$<TARGET_FILE:cpu_ordinal_original_lifted>"
            "$<TARGET_FILE:cpu_ordinal_provider>" "$<TARGET_FILE:cpu_ordinal_altered>"
        --reloc "$<TARGET_FILE:cpu_reloc_original>" "$<TARGET_FILE:cpu_import_provider>"
            "$<TARGET_FILE:cpu_import_altered>"
        --two "$<TARGET_FILE:cpu_two_imports>" "$<TARGET_FILE:cpu_import_provider>"
            "$<TARGET_FILE:cpu_register_probe>")
    add_test(NAME cpu_native_mutation COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_mutation_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_register_probe>" "$<TARGET_FILE:cpu_import_original>"
        --provider "$<TARGET_FILE:cpu_import_provider>")
    add_test(NAME cpu_native_reloc_free COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_reloc_free_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_register_probe>" "$<TARGET_FILE:cpu_import_original>"
        "$<TARGET_FILE:cpu_import_provider>")
    cpu_asm_fixture(cpu_seh_original seh_route)
    add_test(NAME cpu_native_seh COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_seh_check.py" "$<TARGET_FILE:cpu_seh_original>"
        "$<TARGET_FILE:pe_map>" "$<TARGET_FILE:cpu_register_probe>")
    add_test(NAME cpu_native_section COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_section_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_register_probe>")
    add_library(cpu_tls_sink SHARED tests/cpu/tls_sink.cpp)
    foreach(variant IN ITEMS original refuse)
        add_library(cpu_tls_${variant} SHARED tests/cpu/tls_route.asm tests/cpu/tls_route.def)
        set_target_properties(cpu_tls_${variant} PROPERTIES LINKER_LANGUAGE CXX)
        target_link_options(cpu_tls_${variant} PRIVATE /ENTRY:dll_entry /NODEFAULTLIB /INCREMENTAL:NO)
        target_link_libraries(cpu_tls_${variant} PRIVATE cpu_tls_sink)
    endforeach()
    target_compile_definitions(cpu_tls_refuse PRIVATE ENTRY_FAILS)
    add_test(NAME cpu_native_tls COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_tls_check.py" "$<TARGET_FILE:cpu_tls_original>"
        "$<TARGET_FILE:cpu_tls_refuse>" "$<TARGET_FILE:cpu_tls_sink>" "$<TARGET_FILE:pe_map>")
    foreach(variant IN ITEMS original no_tls)
        add_executable(cpu_exe_start_${variant} tests/cpu/exe_start.asm)
        set_target_properties(cpu_exe_start_${variant} PROPERTIES LINKER_LANGUAGE CXX)
        target_link_options(cpu_exe_start_${variant} PRIVATE /ENTRY:exe_entry /NODEFAULTLIB /SUBSYSTEM:CONSOLE
            /DYNAMICBASE /INCREMENTAL:NO)
        target_link_libraries(cpu_exe_start_${variant} PRIVATE cpu_import_provider)
    endforeach()
    target_compile_definitions(cpu_exe_start_no_tls PRIVATE NO_TLS)
    add_test(NAME cpu_native_exe_start COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_exe_start_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_exe_start_original>" "$<TARGET_FILE:cpu_exe_start_no_tls>"
        "$<TARGET_FILE:cpu_import_provider>" "$<TARGET_FILE:cpu_import_altered>")
    # Two CRT-free DLLs and an EXE: A has no imports, B imports A, the EXE imports both.
    foreach(dll IN ITEMS a a_alt b b_noinit)
        string(REGEX REPLACE "_(noinit|alt)$" "" source "${dll}")
        add_library(cpu_startup_${dll} SHARED tests/cpu/startup_${source}.cpp)
        target_compile_options(cpu_startup_${dll} PRIVATE /GS- /Zl)
        target_link_options(cpu_startup_${dll} PRIVATE /ENTRY:${source}_main /NODEFAULTLIB /INCREMENTAL:NO)
        target_link_libraries(cpu_startup_${dll} PRIVATE kernel32)
    endforeach()
    target_compile_definitions(cpu_startup_b_noinit PRIVATE NO_INIT)
    target_compile_definitions(cpu_startup_a_alt PRIVATE ALTER)
    target_link_libraries(cpu_startup_b PRIVATE cpu_startup_a)
    target_link_libraries(cpu_startup_b_noinit PRIVATE cpu_startup_a)
    add_executable(cpu_startup_exe tests/cpu/startup_exe.cpp)
    target_compile_options(cpu_startup_exe PRIVATE /GS- /Zl)
    target_link_options(cpu_startup_exe PRIVATE /ENTRY:exe_entry /NODEFAULTLIB /SUBSYSTEM:CONSOLE /DYNAMICBASE
        /INCREMENTAL:NO)
    target_link_libraries(cpu_startup_exe PRIVATE kernel32 cpu_startup_a cpu_startup_b)
    foreach(control IN ITEMS timeout create)  # controls: a worker that outlives the wait, and one that cannot start
        add_executable(cpu_startup_exe_${control} tests/cpu/startup_exe.cpp)
        string(TOUPPER "${control}" definition)
        target_compile_definitions(cpu_startup_exe_${control} PRIVATE ${definition}_CONTROL)
        target_compile_options(cpu_startup_exe_${control} PRIVATE /GS- /Zl)
        target_link_options(cpu_startup_exe_${control} PRIVATE /ENTRY:exe_entry /NODEFAULTLIB /SUBSYSTEM:CONSOLE
            /DYNAMICBASE /INCREMENTAL:NO)
        target_link_libraries(cpu_startup_exe_${control} PRIVATE kernel32 cpu_startup_a cpu_startup_b)
    endforeach()
    add_test(NAME cpu_native_startup COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_startup_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_startup_exe>" "$<TARGET_FILE:cpu_startup_a>" "$<TARGET_FILE:cpu_startup_b>"
        "$<TARGET_FILE:cpu_startup_b_noinit>" "$<TARGET_FILE:cpu_startup_a_alt>"
        "$<TARGET_FILE:cpu_startup_exe_timeout>" "$<TARGET_FILE:cpu_startup_exe_create>")
    # Lifecycle fixtures: two DLLs with the static MSVC runtime (B imports A) and a CRT-free event sink.
    add_library(cpu_life_sink SHARED tests/cpu/life_sink.cpp)
    target_compile_options(cpu_life_sink PRIVATE /GS- /Zl)
    target_link_options(cpu_life_sink PRIVATE /ENTRY:sink_main /NODEFAULTLIB /INCREMENTAL:NO)
    target_link_libraries(cpu_life_sink PRIVATE kernel32)
    foreach(dll IN ITEMS a a_noctor b b_nodtor)
        string(REGEX REPLACE "_(noctor|nodtor)$" "" source "${dll}")
        add_library(cpu_life_${dll} SHARED tests/cpu/life_${source}.cpp)
        set_target_properties(cpu_life_${dll} PROPERTIES MSVC_RUNTIME_LIBRARY MultiThreaded)
        target_link_options(cpu_life_${dll} PRIVATE /INCREMENTAL:NO)  # no incremental thunk in front of the entry point
        target_link_libraries(cpu_life_${dll} PRIVATE cpu_life_sink)
    endforeach()
    target_compile_definitions(cpu_life_a_noctor PRIVATE NO_CTOR)
    target_compile_definitions(cpu_life_b_nodtor PRIVATE NO_DTOR)
    target_link_libraries(cpu_life_b PRIVATE cpu_life_a)
    target_link_libraries(cpu_life_b_nodtor PRIVATE cpu_life_a)
    add_test(NAME cpu_native_crt_lifecycle COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_crt_lifecycle_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_life_sink>" "$<TARGET_FILE:cpu_life_a>" "$<TARGET_FILE:cpu_life_b>"
        "$<TARGET_FILE:cpu_life_a_noctor>" "$<TARGET_FILE:cpu_life_b_nodtor>")
    add_library(cpu_frame_host SHARED tests/cpu/frame_host.cpp)
    add_library(cpu_frame_guest SHARED tests/cpu/tls_route.asm tests/cpu/frame_route.def)
    set_target_properties(cpu_frame_guest PROPERTIES LINKER_LANGUAGE CXX)
    target_compile_definitions(cpu_frame_guest PRIVATE FRAME_GUEST)
    target_link_options(cpu_frame_guest PRIVATE /ENTRY:dll_entry /NODEFAULTLIB /INCREMENTAL:NO)
    target_link_libraries(cpu_frame_guest PRIVATE cpu_tls_sink cpu_frame_host)

    add_library(cpu_delay_provider SHARED tests/cpu/delay_provider.cpp tests/cpu/delay_provider.def)
    add_library(cpu_delay_provider_altered SHARED tests/cpu/delay_provider.cpp
        tests/cpu/delay_provider_altered.def)
    target_compile_definitions(cpu_delay_provider_altered PRIVATE ALTER_RETURN)
    add_library(cpu_delay_route SHARED tests/cpu/delay_route.cpp)
    target_link_libraries(cpu_delay_route PRIVATE cpu_delay_provider delayimp)
    target_link_options(cpu_delay_route PRIVATE /DELAYLOAD:cpu_delay_provider.dll /INCREMENTAL:NO)
    add_test(NAME cpu_native_delay COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_delay_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_delay_route>" "$<TARGET_FILE:cpu_delay_provider>"
        "$<TARGET_FILE:cpu_delay_provider_altered>")
    add_test(NAME cpu_native_lifetime COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/cpu/native_lifetime_check.py" "$<TARGET_FILE:pe_map>"
        "$<TARGET_FILE:cpu_tls_original>" "$<TARGET_FILE:cpu_tls_sink>")
    add_library(script_path_host SHARED tools/script_path_host.cpp)
    target_compile_features(script_path_host PRIVATE cxx_std_17)
    add_test(NAME script_path_links COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/script_path_check.py" "$<TARGET_FILE:script_path_host>")
    add_executable(ffx_pass26_lds tests/ffx_pass26_lds/gate.cpp)
    target_link_libraries(ffx_pass26_lds PRIVATE d3d11 d3dcompiler)
    add_test(NAME ffx_pass26_lds_warp COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/ffx_pass26_lds/verify.py"
        --host $<TARGET_FILE:ffx_pass26_lds> --backend warp)

    add_test(NAME script_state_literals COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/script_state_check.py")
    add_test(NAME script_dispatch_pairs COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/script_dispatch_check.py" "$<TARGET_FILE:script_path_host>")
    add_test(NAME bcpack_prefix_and_mip_tail COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/bcpack_check.py")
    add_test(NAME bc7_block_reference COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/bc7_blocks_check.py")
    add_test(NAME resource_range_controls COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/resource_ranges_check.py")
    add_test(NAME sgd5_effect_candidates COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/sgd5_effects_check.py")
    add_test(NAME rdna2_tools_check COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/rdna2_tools_check.py")
    add_test(NAME pso_readiness_check COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/pso_readiness_check.py")
    add_test(NAME dxcontainer_part_offsets COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/dxcontainer_part_offsets_check.py")
    foreach(adapter IN ITEMS hardware warp)
        add_test(NAME cpu_native_frame_${adapter} COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/cpu/native_frame_check.py" "$<TARGET_FILE:cpu_frame_guest>"
            "$<TARGET_FILE:cpu_tls_sink>" "$<TARGET_FILE:cpu_frame_host>" "$<TARGET_FILE:pe_map>"
            "${DXC_EXECUTABLE}" "$<TARGET_FILE:offscreen_check>" ${adapter})
        set_tests_properties(cpu_native_frame_${adapter} PROPERTIES TIMEOUT 95)
        add_test(NAME pixel_aot_${adapter} COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/pixel_aot_check.py" draw "${DXC_EXECUTABLE}"
            "$<TARGET_FILE:offscreen_check>" ${adapter})
        set_tests_properties(pixel_aot_${adapter} PROPERTIES TIMEOUT 60)
        add_test(NAME bc7_draw_${adapter} COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/bc7_draw_check.py" "${DXC_EXECUTABLE}"
            "$<TARGET_FILE:offscreen_check>" ${adapter})
        set_tests_properties(bc7_draw_${adapter} PROPERTIES TIMEOUT 60)
    endforeach()
    set_tests_properties(cpu_native_frame_hardware pixel_aot_hardware bc7_draw_hardware PROPERTIES SKIP_RETURN_CODE 77)
    foreach(adapter IN ITEMS hardware warp)
        # The gfx programs this test translated, for tools/form_coverage.py.
        set_tests_properties(pixel_aot_${adapter} PROPERTIES
            ENVIRONMENT "FORM_COVERAGE_RECORD=${CMAKE_BINARY_DIR}/form_coverage/pixel_aot_${adapter}.txt")
    endforeach()
    add_test(NAME compressed_scene_metadata COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/scene_records_check.py" "${CMAKE_CURRENT_BINARY_DIR}/DualUav.cso"
        "${CMAKE_CURRENT_BINARY_DIR}/triangle_ps.cso" "$<TARGET_FILE:root_probe>")
    add_test(NAME root_device_controls_warp COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tools/probe_root_devices.py" --warp-only --bin "$<TARGET_FILE_DIR:root_device_probe>")
    add_test(NAME shader_metadata_probe COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/shader_probe_check.py" "$<TARGET_FILE:root_probe>"
        "${CMAKE_CURRENT_BINARY_DIR}/compute_root.cso" "${CMAKE_CURRENT_BINARY_DIR}/compute_tables.cso"
        "${CMAKE_CURRENT_BINARY_DIR}/triangle_vs.cso" "${DXC_BIN_DIR}/dxil.dll")
    set_tests_properties(shader_metadata_probe PROPERTIES SKIP_RETURN_CODE 77)
    add_test(NAME shader_metadata_controls COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/shader_probe_check.py")
    add_test(NAME dxil_retarget_probe COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/dxil_retarget_check.py" "$<TARGET_FILE:root_probe>"
        "${CMAKE_CURRENT_BINARY_DIR}/compute_root.cso" "${CMAKE_CURRENT_BINARY_DIR}/CopyBlocks.cso"
        "${DXC_BIN_DIR}/dxil.dll")
    set_tests_properties(dxil_retarget_probe PROPERTIES SKIP_RETURN_CODE 77)
    add_test(NAME dxil_retarget_controls COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/dxil_retarget_check.py")
    add_test(NAME conditional_import_paths COMMAND "${INTAKE_PYTHON}" -B "${CMAKE_SOURCE_DIR}/tests/intake/paths_check.py")
    add_library(intake_provider SHARED tests/intake/provider.cpp tests/intake/provider.def)
    add_library(intake_delay SHARED tests/intake/provider.cpp tests/intake/delay.def)
    add_executable(intake_candidate tests/intake/candidate.cpp)
    # Build an import library from a name only. No graphics DLL is linked or executed at build time.
    set(graphics_import "${CMAKE_CURRENT_BINARY_DIR}/intake_graphics.lib")
    add_custom_command(OUTPUT "${graphics_import}"
        COMMAND "${CMAKE_AR}" /nologo /machine:x64 "/def:${CMAKE_CURRENT_SOURCE_DIR}/tests/intake/graphics_import.def"
                "/out:${graphics_import}"
        DEPENDS tests/intake/graphics_import.def VERBATIM)
    target_sources(intake_candidate PRIVATE "${graphics_import}")
    target_link_libraries(intake_candidate PRIVATE intake_provider intake_delay delayimp "${graphics_import}")
    target_link_options(intake_candidate PRIVATE /DELAYLOAD:intake_delay.dll)
    add_test(NAME static_exe_intake COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/intake/check.py" "$<TARGET_FILE:intake_candidate>"
        "$<TARGET_FILE:intake_provider>")
    # intake_forward exports six placeholders. The test rewrites a copy so each export is a forwarder;
    # it reads the images and never loads them.
    add_library(intake_forward SHARED tests/intake/provider.cpp tests/intake/forward.def)
    add_executable(intake_forward_caller tests/intake/forward_caller.cpp)
    target_link_libraries(intake_forward_caller PRIVATE intake_forward)
    add_test(NAME bundled_export_forwarders COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/intake/forwarders_check.py" "$<TARGET_FILE:intake_forward_caller>"
        "$<TARGET_FILE:intake_forward>" "$<TARGET_FILE:intake_provider>")
    add_test(NAME layout_chunk_summary COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/layout_chunks_check.py")
    add_test(NAME dependency_paths_controls COMMAND "${INTAKE_PYTHON}" -B
        "${CMAKE_SOURCE_DIR}/tests/dependency_paths_check.py")
else()
    message(WARNING
        "Python-driven tests, including the CPU fixtures, are disabled because "
        "INTAKE_PYTHON was not found at '${INTAKE_PYTHON}'. "
        "Follow the Python setup under First build in README.md, or set INTAKE_PYTHON "
        "to an interpreter with requirements-dev.txt installed. Then rerun CMake.")
endif()

if(EXISTS "${INTAKE_PYTHON}")
    add_executable(rcas_coordinate_readback tests/rcas_coordinate_readback/readback.cpp)
    target_link_libraries(rcas_coordinate_readback PRIVATE d3d11 d3dcompiler)
    foreach(control IN ITEMS positive negative)
        add_test(NAME rcas_coordinate_${control}_warp COMMAND "${INTAKE_PYTHON}" -B
            "${CMAKE_SOURCE_DIR}/tests/rcas_coordinate_readback/verify.py" "${CMAKE_SOURCE_DIR}"
            --host $<TARGET_FILE:rcas_coordinate_readback> --backend warp --case ${control})
    endforeach()
endif()
