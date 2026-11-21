; Source-owned differential fixture for fixed stack frames in call-free functions.
; Each export is fn(x, y, trace) with x in RCX, y in RDX and a three-word trace in R8, and
; returns RAX. The register probe seeds RBX and RSI, so a save or restore that the
; translation loses shows up as a changed sentinel.
.code

; Nonvolatile registers used as scratch, saved by PUSH and restored by POP.
save_nonvolatile PROC FRAME
    push rbx
    .pushreg rbx
    push rsi
    .pushreg rsi
    .endprolog
    mov rbx, rcx
    mov rsi, rdx
    imul rbx, rsi
    xor rsi, rbx
    mov qword ptr [r8], rbx
    mov qword ptr [r8+8], rsi
    lea rax, [rbx+rsi]
    pop rsi
    pop rbx
    ret
save_nonvolatile ENDP

; A fixed allocation with qword, dword, word and byte stores whose bytes a wider
; reload combines after the source registers are clobbered.
spill_reload PROC FRAME
    sub rsp, 28h
    .allocstack 28h
    .endprolog
    mov qword ptr [rsp+20h], rcx
    mov dword ptr [rsp], edx
    mov dword ptr [rsp+4], ecx
    mov word ptr [rsp+10h], dx
    mov byte ptr [rsp+12h], cl
    mov byte ptr [rsp+13h], 0a5h
    xor ecx, ecx
    xor edx, edx
    mov rax, qword ptr [rsp]
    mov edx, dword ptr [rsp+10h]
    mov rcx, qword ptr [rsp+20h]
    mov qword ptr [r8], rax
    mov qword ptr [r8+8], rdx
    sub rax, rcx
    add rsp, 28h
    ret
spill_reload ENDP

; Spills of RCX and RDX into the caller's home area, with no allocation of its own.
home_spill PROC FRAME
    .endprolog
    mov qword ptr [rsp+8], rcx
    mov qword ptr [rsp+10h], rdx
    mov rcx, r8
    mov rax, qword ptr [rsp+10h]
    add rax, qword ptr [rsp+8]
    mov qword ptr [rcx], rax
    mov rdx, qword ptr [rsp+8]
    mov qword ptr [rcx+8], rdx
    ret
home_spill ENDP

; Both paths write the same frame slots with different values before they join.
join_frame PROC FRAME
    push rdi
    .pushreg rdi
    sub rsp, 20h
    .allocstack 20h
    .endprolog
    mov rdi, rdx
    cmp ecx, edx
    jb short take_y
    mov qword ptr [rsp+8], rcx
    mov dword ptr [rsp+18h], 1
    jmp short joined
take_y:
    mov qword ptr [rsp+8], rdx
    mov dword ptr [rsp+18h], 2
joined:
    mov rax, qword ptr [rsp+8]
    mov ecx, dword ptr [rsp+18h]
    imul rax, rcx
    mov qword ptr [r8], rax
    mov qword ptr [r8+8], rdi
    add rsp, 20h
    pop rdi
    ret
join_frame ENDP

; A frame-resident counter and accumulator carried around a loop's back edge.
loop_counter PROC FRAME
    push rbx
    .pushreg rbx
    sub rsp, 10h
    .allocstack 10h
    .endprolog
    mov dword ptr [rsp], 0
    mov qword ptr [rsp+8], 0
    mov ebx, edx
    and ebx, 15
again:
    cmp dword ptr [rsp], ebx
    jae short done
    mov rax, qword ptr [rsp+8]
    lea rax, [rax+rax*2]
    add rax, rcx
    mov qword ptr [rsp+8], rax
    inc dword ptr [rsp]
    jmp short again
done:
    mov rax, qword ptr [rsp+8]
    mov ecx, dword ptr [rsp]
    mov qword ptr [r8], rcx
    mov qword ptr [r8+8], rbx
    add rsp, 10h
    pop rbx
    ret
loop_counter ENDP

END
